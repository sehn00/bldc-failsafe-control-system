#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L

#include "protocol.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define DEFAULT_UART_DEVICE "/dev/serial0"
#define UART_BAUD B115200
#define HEARTBEAT_INTERVAL_MS 100
#define STATE_INTERVAL_MS 500
#define STATE_TIMEOUT_MS 100
#define ACTION_SETTLE_MS 20
#define UART_TX_TIMEOUT_MS 100
#define CLIENT_TIMEOUT_MS 3000
#define MAX_QUERY_FAILURES 3U
#define CLIENT_INPUT_SIZE 128
#define CLIENT_RESPONSE_SIZE 256
#define LISTEN_BACKLOG 4
#define MAX_CLIENT_READS_PER_TICK 4U
#define MAX_UART_READS_PER_TICK 8U

enum log_level {
    LOG_INFO,
    LOG_WARN,
    LOG_ERROR
};

enum request_type {
    REQUEST_STATUS,
    REQUEST_MODE,
    REQUEST_ENABLE,
    REQUEST_DISABLE,
    REQUEST_TARGET,
    REQUEST_CLEAR_FAULT
};

struct request {
    enum request_type type;
    uint8_t mode;
    uint16_t target;
    uint16_t ramp_ms;
};

enum client_phase {
    CLIENT_NONE,
    CLIENT_READING,
    CLIENT_READY,
    CLIENT_SETTLING,
    CLIENT_WAITING_QUERY
};

struct client {
    int fd;
    enum client_phase phase;
    char input[CLIENT_INPUT_SIZE];
    size_t input_length;
    struct request request;
    int64_t deadline_ms;
    int64_t settle_until_ms;
};

enum query_purpose {
    QUERY_STARTUP,
    QUERY_MONITOR,
    QUERY_CLIENT
};

struct pending_query {
    bool active;
    enum query_purpose purpose;
    int64_t deadline_ms;
};

struct app {
    int lock_fd;
    int uart_fd;
    int listen_fd;
    bool socket_owned;
    struct client client;
    struct pending_query query;
    struct motor_frame_parser parser;
    struct motor_state_snapshot state;
    bool state_valid;
    bool synchronized;
    unsigned int consecutive_query_failures;
    unsigned int crc_errors;
    unsigned int protocol_errors;
    int64_t next_heartbeat_ms;
    int64_t next_monitor_ms;
    bool fatal;
    const char *exit_reason;
};

static volatile sig_atomic_t caught_signal;

static void set_fatal(struct app *app, const char *reason);

static void log_message(enum log_level level, const char *format, ...)
{
    const char *name = "INFO";
    va_list arguments;

    if (level == LOG_WARN) {
        name = "WARN";
    } else if (level == LOG_ERROR) {
        name = "ERROR";
    }

    fprintf(stderr, "motor-supervisor[%s]: ", name);
    va_start(arguments, format);
    vfprintf(stderr, format, arguments);
    va_end(arguments);
    fputc('\n', stderr);
}

static void close_checked(int fd, const char *description)
{
    if (close(fd) != 0) {
        log_message(LOG_WARN, "close %s: %s", description, strerror(errno));
    }
}

static int64_t monotonic_ms(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return -1;
    }
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int milliseconds_until(int64_t now, int64_t deadline)
{
    int64_t remaining = deadline - now;

    if (remaining <= 0) {
        return 0;
    }
    if (remaining > 1000) {
        return 1000;
    }
    return (int)remaining;
}

static void reduce_timeout(int *timeout, int64_t now, int64_t deadline)
{
    int candidate = milliseconds_until(now, deadline);

    if (candidate < *timeout) {
        *timeout = candidate;
    }
}

static void signal_handler(int signal_number)
{
    caught_signal = signal_number;
}

static int install_signal_handlers(void)
{
    struct sigaction action;
    struct sigaction ignore;

    memset(&action, 0, sizeof(action));
    action.sa_handler = signal_handler;
    sigemptyset(&action.sa_mask);
    memset(&ignore, 0, sizeof(ignore));
    ignore.sa_handler = SIG_IGN;
    sigemptyset(&ignore.sa_mask);
    if (sigaction(SIGINT, &action, NULL) != 0 ||
        sigaction(SIGTERM, &action, NULL) != 0 ||
        sigaction(SIGPIPE, &ignore, NULL) != 0) {
        log_message(LOG_ERROR, "install signal handlers: %s", strerror(errno));
        return -1;
    }
    return 0;
}

static int set_nonblocking_cloexec(int fd)
{
    int flags = fcntl(fd, F_GETFL);
    int descriptor_flags;

    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
        return -1;
    }
    descriptor_flags = fcntl(fd, F_GETFD);
    if (descriptor_flags < 0 ||
        fcntl(fd, F_SETFD, descriptor_flags | FD_CLOEXEC) != 0) {
        return -1;
    }
    return 0;
}

static int acquire_process_lock(void)
{
    int fd = open(MOTOR_LOCK_PATH, O_RDWR | O_CREAT | O_CLOEXEC, 0644);

    if (fd < 0) {
        log_message(LOG_ERROR, "open lock %s: %s", MOTOR_LOCK_PATH,
                    strerror(errno));
        return -1;
    }
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
        log_message(LOG_ERROR, "another motor-supervisor is already running");
        close_checked(fd, "process lock");
        return -1;
    }
    return fd;
}

static int open_uart(const char *device)
{
    struct termios attributes;
    int fd = open(device, O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);

    if (fd < 0) {
        log_message(LOG_ERROR, "open UART %s: %s", device, strerror(errno));
        return -1;
    }
    if (ioctl(fd, TIOCEXCL) != 0) {
        log_message(LOG_ERROR, "claim UART %s exclusively: %s", device,
                    strerror(errno));
        close_checked(fd, "UART");
        return -1;
    }
    if (tcgetattr(fd, &attributes) != 0) {
        log_message(LOG_ERROR, "read UART settings: %s", strerror(errno));
        close_checked(fd, "UART");
        return -1;
    }

    attributes.c_iflag = 0;
    attributes.c_oflag = 0;
    attributes.c_lflag = 0;
    attributes.c_cflag &= ~(CSIZE | PARENB | PARODD | CSTOPB);
#ifdef CRTSCTS
    attributes.c_cflag &= ~CRTSCTS;
#endif
    attributes.c_cflag |= CS8 | CLOCAL | CREAD;
    attributes.c_cc[VMIN] = 0;
    attributes.c_cc[VTIME] = 0;
    if (cfsetispeed(&attributes, UART_BAUD) != 0 ||
        cfsetospeed(&attributes, UART_BAUD) != 0 ||
        tcsetattr(fd, TCSANOW, &attributes) != 0) {
        log_message(LOG_ERROR, "configure UART %s: %s", device,
                    strerror(errno));
        close_checked(fd, "UART");
        return -1;
    }
    if (tcflush(fd, TCIOFLUSH) != 0) {
        log_message(LOG_ERROR, "flush UART %s at startup: %s", device,
                    strerror(errno));
        close_checked(fd, "UART");
        return -1;
    }

    log_message(LOG_INFO,
                "UART open device=%s baud=115200 format=8N1 flow=none", device);
    return fd;
}

static int create_listener(struct app *app)
{
    struct sockaddr_un address;
    struct stat path_status;
    mode_t previous_umask;
    int fd;

    if (strlen(MOTOR_SOCKET_PATH) >= sizeof(address.sun_path)) {
        log_message(LOG_ERROR, "socket path is too long: %s", MOTOR_SOCKET_PATH);
        return -1;
    }
    if (lstat(MOTOR_SOCKET_PATH, &path_status) == 0) {
        if (!S_ISSOCK(path_status.st_mode)) {
            log_message(LOG_ERROR, "refusing to replace non-socket %s",
                        MOTOR_SOCKET_PATH);
            return -1;
        }
        if (unlink(MOTOR_SOCKET_PATH) != 0) {
            log_message(LOG_ERROR, "remove stale socket %s: %s",
                        MOTOR_SOCKET_PATH, strerror(errno));
            return -1;
        }
    } else if (errno != ENOENT) {
        log_message(LOG_ERROR, "inspect socket path %s: %s", MOTOR_SOCKET_PATH,
                    strerror(errno));
        return -1;
    }

    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        log_message(LOG_ERROR, "create Unix socket: %s", strerror(errno));
        return -1;
    }
    if (set_nonblocking_cloexec(fd) != 0) {
        log_message(LOG_ERROR, "configure Unix socket: %s", strerror(errno));
        close_checked(fd, "Unix listener");
        return -1;
    }

    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, MOTOR_SOCKET_PATH, strlen(MOTOR_SOCKET_PATH) + 1);
    previous_umask = umask(0117);
    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        int saved_errno = errno;

        (void)umask(previous_umask);
        errno = saved_errno;
        log_message(LOG_ERROR, "bind Unix socket %s: %s", MOTOR_SOCKET_PATH,
                    strerror(errno));
        close_checked(fd, "Unix listener");
        return -1;
    }
    (void)umask(previous_umask);
    app->socket_owned = true;
    if (listen(fd, LISTEN_BACKLOG) != 0) {
        log_message(LOG_ERROR, "listen on Unix socket: %s", strerror(errno));
        close_checked(fd, "Unix listener");
        return -1;
    }
    return fd;
}

static bool parse_unsigned(const char *text, unsigned long maximum,
                           unsigned long *value)
{
    const unsigned char *cursor = (const unsigned char *)text;
    char *end;
    unsigned long parsed;

    if (*cursor == '\0') {
        return false;
    }
    while (*cursor != '\0') {
        if (!isdigit(*cursor)) {
            return false;
        }
        ++cursor;
    }
    errno = 0;
    parsed = strtoul(text, &end, 10);
    if (errno != 0 || *end != '\0' || parsed > maximum) {
        return false;
    }
    *value = parsed;
    return true;
}

static int parse_request(char *line, struct request *request, char *error,
                         size_t error_size)
{
    char *save = NULL;
    char *command = strtok_r(line, " \t", &save);
    char *first = strtok_r(NULL, " \t", &save);
    char *second = strtok_r(NULL, " \t", &save);
    char *extra = strtok_r(NULL, " \t", &save);
    unsigned long value;

    memset(request, 0, sizeof(*request));
    if (command == NULL) {
        snprintf(error, error_size, "empty command");
        return -1;
    }
    if (strcmp(command, "STATUS") == 0 && first == NULL) {
        request->type = REQUEST_STATUS;
        return 0;
    }
    if (strcmp(command, "ENABLE") == 0 && first == NULL) {
        request->type = REQUEST_ENABLE;
        return 0;
    }
    if (strcmp(command, "DISABLE") == 0 && first == NULL) {
        request->type = REQUEST_DISABLE;
        return 0;
    }
    if (strcmp(command, "CLEAR_FAULT") == 0 && first == NULL) {
        request->type = REQUEST_CLEAR_FAULT;
        return 0;
    }
    if (strcmp(command, "MODE") == 0 && first != NULL && second == NULL) {
        request->type = REQUEST_MODE;
        if (strcmp(first, "LOCAL") == 0) {
            request->mode = MOTOR_MODE_LOCAL;
            return 0;
        }
        if (strcmp(first, "REMOTE") == 0) {
            request->mode = MOTOR_MODE_REMOTE;
            return 0;
        }
        snprintf(error, error_size, "MODE must be LOCAL or REMOTE");
        return -1;
    }
    if (strcmp(command, "TARGET") == 0 && first != NULL && second != NULL &&
        extra == NULL) {
        request->type = REQUEST_TARGET;
        if (!parse_unsigned(first, 100, &value)) {
            snprintf(error, error_size, "target percent must be 0..100");
            return -1;
        }
        request->target = (uint16_t)value;
        if (!parse_unsigned(second, 10000, &value)) {
            snprintf(error, error_size, "ramp_ms must be 0..10000");
            return -1;
        }
        request->ramp_ms = (uint16_t)value;
        return 0;
    }

    snprintf(error, error_size, "invalid command or arguments");
    return -1;
}

static void close_client(struct app *app)
{
    if (app->client.fd >= 0) {
        close_checked(app->client.fd, "client socket");
    }
    memset(&app->client, 0, sizeof(app->client));
    app->client.fd = -1;
    app->client.phase = CLIENT_NONE;
}

static void client_response(struct app *app, const char *format, ...)
{
    char response[CLIENT_RESPONSE_SIZE];
    va_list arguments;
    int written;
    size_t length;
    size_t offset = 0;
    unsigned int attempts;

    if (app->client.fd < 0) {
        return;
    }
    va_start(arguments, format);
    written = vsnprintf(response, sizeof(response) - 1, format, arguments);
    va_end(arguments);
    if (written < 0) {
        close_client(app);
        return;
    }
    length = (size_t)written;
    if (length >= sizeof(response) - 1) {
        length = sizeof(response) - 2;
    }
    response[length++] = '\n';

    for (attempts = 0;
         attempts < CLIENT_RESPONSE_SIZE && offset < length; ++attempts) {
        ssize_t count = send(app->client.fd, &response[offset], length - offset,
                             MSG_NOSIGNAL);

        if (count > 0) {
            offset += (size_t)count;
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count == 0) {
            errno = EPIPE;
        }
        break;
    }
    if (offset < length) {
        log_message(LOG_WARN, "send client response: %s", strerror(errno));
    }
    close_client(app);
}

static void finish_client_line(struct app *app)
{
    char error[96];

    while (app->client.input_length > 0 &&
           app->client.input[app->client.input_length - 1] == '\r') {
        --app->client.input_length;
    }
    app->client.input[app->client.input_length] = '\0';
    if (parse_request(app->client.input, &app->client.request, error,
                      sizeof(error)) != 0) {
        client_response(app, "ERROR %s", error);
        return;
    }
    app->client.phase = CLIENT_READY;
}

static void read_client(struct app *app)
{
    uint8_t input[64];
    unsigned int reads;

    for (reads = 0; reads < MAX_CLIENT_READS_PER_TICK; ++reads) {
        ssize_t count = recv(app->client.fd, input, sizeof(input), 0);
        size_t i;

        if (count > 0) {
            for (i = 0; i < (size_t)count; ++i) {
                if (input[i] == '\0') {
                    client_response(app, "ERROR NUL is not allowed");
                    return;
                }
                if (input[i] == '\n') {
                    size_t trailing;

                    finish_client_line(app);
                    if (app->client.phase != CLIENT_READY) {
                        return;
                    }
                    for (trailing = i + 1; trailing < (size_t)count;
                         ++trailing) {
                        if (!isspace(input[trailing])) {
                            client_response(app,
                                            "ERROR one command per connection");
                            return;
                        }
                    }
                    return;
                }
                if (app->client.input_length >= sizeof(app->client.input) - 1) {
                    client_response(app, "ERROR command line is too long");
                    return;
                }
                app->client.input[app->client.input_length++] = (char)input[i];
            }
            continue;
        }
        if (count == 0) {
            close_client(app);
            return;
        }
        if (errno == EINTR) {
            continue;
        }
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            close_client(app);
        }
        return;
    }
}

static void accept_clients(struct app *app)
{
    unsigned int attempts;

    for (attempts = 0; attempts < LISTEN_BACKLOG; ++attempts) {
        int fd = accept(app->listen_fd, NULL, NULL);

        if (fd < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (errno != EAGAIN && errno != EWOULDBLOCK) {
                log_message(LOG_WARN, "accept client: %s", strerror(errno));
            }
            return;
        }
        if (set_nonblocking_cloexec(fd) != 0) {
            log_message(LOG_WARN, "configure client socket: %s",
                        strerror(errno));
            close_checked(fd, "client socket");
            continue;
        }
        if (app->client.fd >= 0) {
            static const char busy[] = "ERROR BUSY command in progress\n";
            ssize_t count = send(fd, busy, sizeof(busy) - 1, MSG_NOSIGNAL);

            if (count != (ssize_t)(sizeof(busy) - 1)) {
                log_message(LOG_WARN, "send BUSY response failed");
            }
            close_checked(fd, "busy client socket");
            continue;
        }
        memset(&app->client, 0, sizeof(app->client));
        app->client.fd = fd;
        app->client.phase = CLIENT_READING;
        app->client.deadline_ms = monotonic_ms();
        if (app->client.deadline_ms < 0) {
            close_client(app);
            set_fatal(app, "monotonic clock failure");
            return;
        }
        app->client.deadline_ms += CLIENT_TIMEOUT_MS;
    }
}

static int wait_uart_writable(int fd, int64_t deadline_ms)
{
    struct pollfd poll_fd;

    poll_fd.fd = fd;
    poll_fd.events = POLLOUT;
    for (;;) {
        int64_t now = monotonic_ms();
        int result;

        if (now < 0 || now >= deadline_ms) {
            errno = ETIMEDOUT;
            return -1;
        }
        poll_fd.revents = 0;
        result = poll(&poll_fd, 1, milliseconds_until(now, deadline_ms));
        if (result > 0 && (poll_fd.revents & POLLOUT) != 0) {
            return 0;
        }
        if (result > 0) {
            errno = EIO;
            return -1;
        }
        if (result == 0) {
            errno = ETIMEDOUT;
            return -1;
        }
        if (errno != EINTR) {
            return -1;
        }
    }
}

static int write_frame_bytes(struct app *app,
                             const uint8_t frame[MOTOR_FRAME_SIZE])
{
    size_t offset = 0;
    int64_t now = monotonic_ms();
    int64_t deadline;

    if (now < 0) {
        errno = EINVAL;
        return -1;
    }
    deadline = now + UART_TX_TIMEOUT_MS;
    while (offset < MOTOR_FRAME_SIZE) {
        ssize_t count = write(app->uart_fd, &frame[offset],
                              MOTOR_FRAME_SIZE - offset);

        if (count > 0) {
            offset += (size_t)count;
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (wait_uart_writable(app->uart_fd, deadline) == 0) {
                continue;
            }
        }
        if (count == 0) {
            errno = EIO;
        }
        return -1;
    }
    return 0;
}

static int write_uart_frame(struct app *app, uint8_t command,
                            const uint8_t data[MOTOR_DATA_SIZE])
{
    uint8_t frame[MOTOR_FRAME_SIZE];

    if (data == NULL) {
        errno = EINVAL;
        return -1;
    }
    switch (command) {
    case MOTOR_CMD_GET_STATE:
    case MOTOR_CMD_SET_MODE:
    case MOTOR_CMD_ENABLE:
    case MOTOR_CMD_DISABLE:
    case MOTOR_CMD_SET_TARGET:
    case MOTOR_CMD_HEARTBEAT:
    case MOTOR_CMD_CLEAR_FAULT:
        break;
    default:
        errno = EINVAL;
        return -1;
    }
    motor_frame_build(frame, command, data);
    return write_frame_bytes(app, frame);
}


static void set_fatal(struct app *app, const char *reason)
{
    if (!app->fatal) {
        app->fatal = true;
        app->exit_reason = reason;
    }
}

static int start_query(struct app *app, enum query_purpose purpose)
{
    static const uint8_t zero[MOTOR_DATA_SIZE] = {0, 0, 0, 0};
    int64_t sent_at;

    if (app->query.active) {
        return -1;
    }
    if (write_uart_frame(app, MOTOR_CMD_GET_STATE, zero) != 0) {
        log_message(LOG_ERROR, "write GET_STATE: %s", strerror(errno));
        set_fatal(app, "UART write failure");
        return -1;
    }
    sent_at = monotonic_ms();
    if (sent_at < 0) {
        set_fatal(app, "monotonic clock failure");
        return -1;
    }
    app->query.active = true;
    app->query.purpose = purpose;
    app->query.deadline_ms = sent_at + STATE_TIMEOUT_MS;
    app->next_monitor_ms = sent_at + STATE_INTERVAL_MS;
    return 0;
}

static void format_state(const struct motor_state_snapshot *state, char *text,
                         size_t text_size)
{
    snprintf(text, text_size, "STATE=%s MODE=%s FAULT=%s",
             motor_state_name(state->state), motor_mode_name(state->mode),
             motor_fault_name(state->fault));
}

static void update_state(struct app *app,
                         const struct motor_state_snapshot *state)
{
    bool initial = !app->state_valid;
    bool changed = initial || app->state.state != state->state ||
                   app->state.mode != state->mode ||
                   app->state.fault != state->fault;

    app->state = *state;
    app->state_valid = true;
    if (changed) {
        log_message(LOG_INFO, "%s STM32 STATE=%s MODE=%s FAULT=%s",
                    initial ? "initial" : "changed",
                    motor_state_name(state->state),
                    motor_mode_name(state->mode),
                    motor_fault_name(state->fault));
    }
}

static void respond_to_completed_request(struct app *app)
{
    char state_text[128];
    const struct request *request = &app->client.request;

    if (app->client.fd < 0 || app->client.phase != CLIENT_WAITING_QUERY) {
        return;
    }
    format_state(&app->state, state_text, sizeof(state_text));
    switch (request->type) {
    case REQUEST_STATUS:
        client_response(app, "OK %s", state_text);
        break;
    case REQUEST_MODE:
        if (app->state.mode == request->mode) {
            client_response(app, "OK %s", state_text);
        } else {
            client_response(app, "ERROR requested mode was not observed; %s",
                            state_text);
        }
        break;
    case REQUEST_ENABLE:
        if (app->state.state == MOTOR_STATE_RUN) {
            client_response(app, "OK %s", state_text);
        } else {
            client_response(app, "ERROR ENABLE did not result in RUN; %s",
                            state_text);
        }
        break;
    case REQUEST_DISABLE:
        if (app->state.state != MOTOR_STATE_RUN) {
            client_response(app, "OK %s", state_text);
        } else {
            client_response(app, "ERROR DISABLE left state RUN; %s",
                            state_text);
        }
        break;
    case REQUEST_CLEAR_FAULT:
        if (app->state.state == MOTOR_STATE_READY &&
            app->state.fault == MOTOR_FAULT_NONE) {
            client_response(app, "OK %s", state_text);
        } else {
            client_response(app, "ERROR CLEAR_FAULT did not reach READY; %s",
                            state_text);
        }
        break;
    case REQUEST_TARGET:
        client_response(app, "WARN TARGET_SENT_NO_ACK %s", state_text);
        break;
    default:
        client_response(app, "ERROR invalid internal request state");
        set_fatal(app, "invalid internal request state");
        break;
    }
}

static void complete_query(struct app *app,
                           const struct motor_state_snapshot *state,
                           int64_t now)
{
    enum query_purpose purpose = app->query.purpose;

    app->query.active = false;
    app->consecutive_query_failures = 0;
    app->synchronized = true;
    app->next_monitor_ms = now + STATE_INTERVAL_MS;
    update_state(app, state);
    if (purpose == QUERY_CLIENT) {
        respond_to_completed_request(app);
    } else if (purpose != QUERY_STARTUP && purpose != QUERY_MONITOR) {
        set_fatal(app, "invalid internal query state");
    }
}

static void process_uart_frame(struct app *app,
                               const uint8_t frame[MOTOR_FRAME_SIZE])
{
    struct motor_state_snapshot state;
    int64_t now;

    if (frame[1] != MOTOR_CMD_RSP_STATE ||
        motor_frame_decode_state(frame, &state) != 0) {
        ++app->protocol_errors;
        return;
    }
    now = monotonic_ms();
    if (now < 0) {
        set_fatal(app, "monotonic clock failure");
        return;
    }
    if (app->query.active && now <= app->query.deadline_ms) {
        complete_query(app, &state, now);
        return;
    }
    update_state(app, &state);
    if (app->query.active) {
        log_message(LOG_WARN, "late RSP_STATE ignored for pending GET_STATE");
    } else {
        ++app->protocol_errors;
    }
}

static void read_uart(struct app *app)
{
    uint8_t input[128];
    unsigned int reads;

    for (reads = 0; reads < MAX_UART_READS_PER_TICK; ++reads) {
        ssize_t count = read(app->uart_fd, input, sizeof(input));
        size_t i;

        if (count > 0) {
            for (i = 0; i < (size_t)count; ++i) {
                uint8_t frame[MOTOR_FRAME_SIZE];
                int result = motor_frame_parser_push(&app->parser, input[i],
                                                     frame);

                if (result == 1) {
                    process_uart_frame(app, frame);
                } else if (result < 0) {
                    ++app->crc_errors;
                }
            }
            continue;
        }
        if (count == 0) {
            /* VMIN=0 permits a zero-byte read; POLLHUP/EIO detects removal. */
            return;
        }
        if (errno == EINTR) {
            continue;
        }
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            log_message(LOG_ERROR, "UART read: %s", strerror(errno));
            set_fatal(app, "UART read failure");
        }
        return;
    }
}

static int request_frame(const struct request *request, uint8_t *command,
                         uint8_t data[MOTOR_DATA_SIZE])
{
    if (request == NULL || command == NULL || data == NULL) {
        return -1;
    }
    memset(data, 0, MOTOR_DATA_SIZE);
    switch (request->type) {
    case REQUEST_MODE:
        if (request->mode > MOTOR_MODE_REMOTE) {
            return -1;
        }
        *command = MOTOR_CMD_SET_MODE;
        data[0] = request->mode;
        break;
    case REQUEST_ENABLE:
        *command = MOTOR_CMD_ENABLE;
        break;
    case REQUEST_DISABLE:
        *command = MOTOR_CMD_DISABLE;
        break;
    case REQUEST_TARGET:
        if (request->target > 100U || request->ramp_ms > 10000U) {
            return -1;
        }
        *command = MOTOR_CMD_SET_TARGET;
        data[0] = (uint8_t)(request->target & 0xFFu);
        data[1] = (uint8_t)(request->target >> 8);
        data[2] = (uint8_t)(request->ramp_ms & 0xFFu);
        data[3] = (uint8_t)(request->ramp_ms >> 8);
        break;
    case REQUEST_CLEAR_FAULT:
        *command = MOTOR_CMD_CLEAR_FAULT;
        break;
    case REQUEST_STATUS:
        return -1;
    default:
        return -1;
    }
    return 0;
}

static void handle_query_timeout(struct app *app, int64_t now)
{
    enum query_purpose purpose = app->query.purpose;

    app->query.active = false;
    app->synchronized = false;
    ++app->consecutive_query_failures;
    log_message(LOG_WARN, "GET_STATE timeout (%u/%u consecutive)",
                app->consecutive_query_failures, MAX_QUERY_FAILURES);
    if (purpose == QUERY_CLIENT && app->client.fd >= 0 &&
        app->client.phase == CLIENT_WAITING_QUERY) {
        client_response(app, "ERROR GET_STATE timed out");
    }
    app->next_monitor_ms = now + STATE_INTERVAL_MS;
    if (app->consecutive_query_failures >= MAX_QUERY_FAILURES) {
        set_fatal(app, "three consecutive GET_STATE timeouts");
    }
}

static void send_heartbeat(struct app *app, int64_t now)
{
    static const uint8_t zero[MOTOR_DATA_SIZE] = {0, 0, 0, 0};

    if (write_uart_frame(app, MOTOR_CMD_HEARTBEAT, zero) != 0) {
        log_message(LOG_ERROR, "write HEARTBEAT: %s", strerror(errno));
        set_fatal(app, "UART write failure");
        return;
    }
    app->next_heartbeat_ms = now + HEARTBEAT_INTERVAL_MS;
}

static void schedule_work(struct app *app, int64_t now)
{
    uint8_t command = MOTOR_CMD_GET_STATE;
    uint8_t data[MOTOR_DATA_SIZE];

    if (app->fatal || caught_signal != 0) {
        return;
    }
    if (now >= app->next_heartbeat_ms) {
        send_heartbeat(app, now);
        return;
    }
    if (app->client.phase == CLIENT_SETTLING &&
        now >= app->client.settle_until_ms && !app->query.active) {
        app->client.phase = CLIENT_WAITING_QUERY;
        (void)start_query(app, QUERY_CLIENT);
        return;
    }
    if (app->client.phase == CLIENT_READY && !app->query.active) {
        if (app->client.request.type == REQUEST_STATUS) {
            app->client.phase = CLIENT_WAITING_QUERY;
            (void)start_query(app, QUERY_CLIENT);
            return;
        }
        if (!app->synchronized &&
            app->client.request.type != REQUEST_DISABLE) {
            client_response(app,
                            "ERROR supervisor has not synchronized STM32 state");
            return;
        }
        request_frame(&app->client.request, &command, data);
        if (write_uart_frame(app, command, data) != 0) {
            log_message(LOG_ERROR, "write command 0x%02X: %s", command,
                        strerror(errno));
            client_response(app, "ERROR UART write failed");
            set_fatal(app, "UART write failure");
            return;
        }
        app->client.phase = CLIENT_SETTLING;
        app->client.settle_until_ms = now + ACTION_SETTLE_MS;
        return;
    }
    if (!app->query.active && now >= app->next_monitor_ms) {
        (void)start_query(app, QUERY_MONITOR);
    }
}

static void process_deadlines(struct app *app, int64_t now)
{
    if (app->query.active && now >= app->query.deadline_ms) {
        handle_query_timeout(app, now);
    }
    if (app->client.fd >= 0 && now >= app->client.deadline_ms) {
        client_response(app, "ERROR client command timed out");
    }
}

static int event_timeout(const struct app *app, int64_t now)
{
    int timeout = 1000;

    reduce_timeout(&timeout, now, app->next_heartbeat_ms);
    if (app->query.active) {
        reduce_timeout(&timeout, now, app->query.deadline_ms);
    } else {
        reduce_timeout(&timeout, now, app->next_monitor_ms);
    }
    if (app->client.fd >= 0) {
        reduce_timeout(&timeout, now, app->client.deadline_ms);
        if (app->client.phase == CLIENT_READY) {
            timeout = 0;
        } else if (app->client.phase == CLIENT_SETTLING) {
            reduce_timeout(&timeout, now, app->client.settle_until_ms);
        }
    }
    return timeout;
}

static int run_event_loop(struct app *app)
{
    while (!app->fatal && caught_signal == 0) {
        struct pollfd fds[3];
        int client_poll_fd = -1;
        int64_t now = monotonic_ms();
        int result;

        if (now < 0) {
            set_fatal(app, "monotonic clock failure");
            break;
        }
        process_deadlines(app, now);
        schedule_work(app, now);
        if (app->fatal || caught_signal != 0) {
            break;
        }

        fds[0].fd = app->uart_fd;
        fds[0].events = POLLIN;
        fds[0].revents = 0;
        fds[1].fd = app->listen_fd;
        fds[1].events = POLLIN;
        fds[1].revents = 0;
        fds[2].fd = -1;
        fds[2].events = 0;
        fds[2].revents = 0;
        if (app->client.fd >= 0 && app->client.phase == CLIENT_READING) {
            client_poll_fd = app->client.fd;
            fds[2].fd = client_poll_fd;
            fds[2].events = POLLIN;
        }

        result = poll(fds, 3, event_timeout(app, monotonic_ms()));
        if (result < 0) {
            if (errno == EINTR) {
                continue;
            }
            log_message(LOG_ERROR, "poll: %s", strerror(errno));
            set_fatal(app, "poll failure");
            break;
        }
        if ((fds[0].revents & POLLIN) != 0) {
            read_uart(app);
        }
        if ((fds[0].revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
            log_message(LOG_ERROR, "UART poll disconnect/error revents=0x%X",
                        fds[0].revents);
            set_fatal(app, "UART disconnected");
        }
        if ((fds[1].revents & POLLIN) != 0) {
            accept_clients(app);
        }
        if ((fds[1].revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
            set_fatal(app, "Unix socket listener failure");
        }
        if (client_poll_fd >= 0 && app->client.fd == client_poll_fd) {
            if ((fds[2].revents & POLLIN) != 0) {
                read_client(app);
            }
            if (app->client.fd == client_poll_fd &&
                (fds[2].revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
                close_client(app);
            }
        }
    }
    return app->fatal ? EXIT_FAILURE : EXIT_SUCCESS;
}

static void best_effort_disable(struct app *app)
{
    static const uint8_t zero[MOTOR_DATA_SIZE] = {0, 0, 0, 0};

    if (app->uart_fd >= 0 &&
        write_uart_frame(app, MOTOR_CMD_DISABLE, zero) != 0) {
        log_message(LOG_WARN, "best-effort DISABLE failed: %s", strerror(errno));
    }
}

static void initialize_app(struct app *app)
{
    memset(app, 0, sizeof(*app));
    app->lock_fd = -1;
    app->uart_fd = -1;
    app->listen_fd = -1;
    app->client.fd = -1;
    app->client.phase = CLIENT_NONE;
    app->exit_reason = "normal shutdown";
    motor_frame_parser_init(&app->parser);
}

static void cleanup_app(struct app *app)
{
    close_client(app);
    if (app->listen_fd >= 0) {
        close(app->listen_fd);
        app->listen_fd = -1;
    }
    if (app->socket_owned) {
        (void)unlink(MOTOR_SOCKET_PATH);
        app->socket_owned = false;
    }
    if (app->uart_fd >= 0) {
        close(app->uart_fd);
        app->uart_fd = -1;
    }
    if (app->lock_fd >= 0) {
        close(app->lock_fd);
        app->lock_fd = -1;
    }
}

static void usage(FILE *stream, const char *program)
{
    fprintf(stream, "Usage: %s [--device UART_DEVICE]\n", program);
}

int main(int argc, char **argv)
{
    const char *device = DEFAULT_UART_DEVICE;
    struct app app;
    int64_t now;
    int result = EXIT_FAILURE;
    int argument;

    for (argument = 1; argument < argc; ++argument) {
        if (strcmp(argv[argument], "--device") == 0 && argument + 1 < argc) {
            device = argv[++argument];
        } else if (strcmp(argv[argument], "--help") == 0) {
            usage(stdout, argv[0]);
            return EXIT_SUCCESS;
        } else {
            usage(stderr, argv[0]);
            return EXIT_FAILURE;
        }
    }

    (void)setvbuf(stderr, NULL, _IOLBF, 0);
    initialize_app(&app);
    if (install_signal_handlers() != 0) {
        return EXIT_FAILURE;
    }
    app.lock_fd = acquire_process_lock();
    if (app.lock_fd < 0) {
        goto finish;
    }
    app.uart_fd = open_uart(device);
    if (app.uart_fd < 0) {
        goto finish;
    }
    app.listen_fd = create_listener(&app);
    if (app.listen_fd < 0) {
        goto finish;
    }

    now = monotonic_ms();
    if (now < 0) {
        app.exit_reason = "monotonic clock failure";
        goto finish;
    }
    app.next_heartbeat_ms = now + HEARTBEAT_INTERVAL_MS;
    app.next_monitor_ms = now + STATE_INTERVAL_MS;
    log_message(LOG_INFO, "requesting initial STM32 state");
    if (start_query(&app, QUERY_STARTUP) == 0) {
        result = run_event_loop(&app);
    }

    if (caught_signal != 0) {
        log_message(LOG_INFO, "received signal %d; sending best-effort DISABLE",
                    caught_signal);
        best_effort_disable(&app);
        app.exit_reason = "SIGINT/SIGTERM shutdown";
        result = EXIT_SUCCESS;
    }

finish:
    log_message(result == EXIT_SUCCESS ? LOG_INFO : LOG_ERROR,
                "exit reason=%s crc_errors=%u protocol_errors=%u",
                app.exit_reason, app.crc_errors, app.protocol_errors);
    cleanup_app(&app);
    return result;
}
