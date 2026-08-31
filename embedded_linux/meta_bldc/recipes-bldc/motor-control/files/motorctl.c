#define _POSIX_C_SOURCE 200809L

#include "protocol.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define COMMAND_CAPACITY 96
#define RESPONSE_CAPACITY 512
#define IO_TIMEOUT_MS 3000

static void close_checked(int fd)
{
    if (close(fd) != 0) {
        fprintf(stderr, "motorctl: close socket: %s\n", strerror(errno));
    }
}

static int ignore_sigpipe(void)
{
    struct sigaction action;

    memset(&action, 0, sizeof(action));
    action.sa_handler = SIG_IGN;
    if (sigemptyset(&action.sa_mask) != 0 ||
        sigaction(SIGPIPE, &action, NULL) != 0) {
        fprintf(stderr, "motorctl: ignore SIGPIPE: %s\n", strerror(errno));
        return -1;
    }
    return 0;
}

static void usage(FILE *stream, const char *program)
{
    fprintf(stream,
            "Usage:\n"
            "  %s status\n"
            "  %s mode local|remote\n"
            "  %s enable\n"
            "  %s disable\n"
            "  %s target <percent 0..100> <ramp_ms 0..10000>\n"
            "  %s clear-fault\n"
            , program, program, program, program, program, program
            );
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

static int build_command(int argc, char **argv, char *command,
                         size_t command_size)
{
    unsigned long target;
    unsigned long ramp;

    if (argc == 2 && strcmp(argv[1], "status") == 0) {
        return snprintf(command, command_size, "STATUS\n");
    }
    if (argc == 2 && strcmp(argv[1], "enable") == 0) {
        return snprintf(command, command_size, "ENABLE\n");
    }
    if (argc == 2 && strcmp(argv[1], "disable") == 0) {
        return snprintf(command, command_size, "DISABLE\n");
    }
    if (argc == 2 && strcmp(argv[1], "clear-fault") == 0) {
        return snprintf(command, command_size, "CLEAR_FAULT\n");
    }
    if (argc == 3 && strcmp(argv[1], "mode") == 0) {
        if (strcmp(argv[2], "local") == 0) {
            return snprintf(command, command_size, "MODE LOCAL\n");
        }
        if (strcmp(argv[2], "remote") == 0) {
            return snprintf(command, command_size, "MODE REMOTE\n");
        }
        return -1;
    }
    if (argc == 4 && strcmp(argv[1], "target") == 0 &&
        parse_unsigned(argv[2], 100, &target) &&
        parse_unsigned(argv[3], 10000, &ramp)) {
        return snprintf(command, command_size, "TARGET %lu %lu\n", target,
                        ramp);
    }
    return -1;
}

static int64_t monotonic_milliseconds(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return -1;
    }
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int wait_for_socket(int fd, short events, int64_t deadline)
{
    struct pollfd poll_fd;

    poll_fd.fd = fd;
    poll_fd.events = events;
    for (;;) {
        int64_t now = monotonic_milliseconds();
        int remaining;
        int result;

        if (now < 0) {
            errno = EIO;
            return -1;
        }
        if (now >= deadline) {
            errno = ETIMEDOUT;
            return 0;
        }
        remaining = (int)(deadline - now);
        poll_fd.revents = 0;
        result = poll(&poll_fd, 1, remaining);
        if (result > 0) {
            return 1;
        }
        if (result == 0) {
            errno = ETIMEDOUT;
            return 0;
        }
        if (errno != EINTR) {
            return -1;
        }
    }
}

static int connect_supervisor(void)
{
    struct sockaddr_un address;
    int fd;

    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        fprintf(stderr, "motorctl: socket: %s\n", strerror(errno));
        return -1;
    }
    {
        int flags = fcntl(fd, F_GETFL);

        if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
            fprintf(stderr, "motorctl: configure socket: %s\n",
                    strerror(errno));
            close_checked(fd);
            return -1;
        }
        flags = fcntl(fd, F_GETFD);
        if (flags < 0 || fcntl(fd, F_SETFD, flags | FD_CLOEXEC) != 0) {
            fprintf(stderr, "motorctl: configure socket: %s\n",
                    strerror(errno));
            close_checked(fd);
            return -1;
        }
    }

    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    if (strlen(MOTOR_SOCKET_PATH) >= sizeof(address.sun_path)) {
        fprintf(stderr, "motorctl: socket path is too long\n");
        close_checked(fd);
        return -1;
    }
    memcpy(address.sun_path, MOTOR_SOCKET_PATH, strlen(MOTOR_SOCKET_PATH) + 1);

    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        int connect_errno = errno;

        if (connect_errno == EINPROGRESS || connect_errno == EINTR ||
            connect_errno == EAGAIN ||
            connect_errno == EWOULDBLOCK) {
            int64_t now = monotonic_milliseconds();
            int socket_error = 0;
            socklen_t error_size = sizeof(socket_error);

            if (now < 0 || wait_for_socket(fd, POLLOUT, now + IO_TIMEOUT_MS) <= 0 ||
                getsockopt(fd, SOL_SOCKET, SO_ERROR, &socket_error,
                           &error_size) != 0 || socket_error != 0) {
                if (socket_error != 0) {
                    errno = socket_error;
                }
                fprintf(stderr, "motorctl: connect %s: %s\n",
                        MOTOR_SOCKET_PATH, strerror(errno));
                close_checked(fd);
                return -1;
            }
        } else {
            fprintf(stderr, "motorctl: connect %s: %s\n", MOTOR_SOCKET_PATH,
                    strerror(connect_errno));
            close_checked(fd);
            return -1;
        }
    }
    return fd;
}

static int send_all(int fd, const char *data, size_t length)
{
    size_t offset = 0;
    int64_t now = monotonic_milliseconds();
    int64_t deadline;

    if (data == NULL && length != 0) {
        errno = EINVAL;
        fprintf(stderr, "motorctl: invalid send buffer\n");
        return -1;
    }
    if (now < 0) {
        fprintf(stderr, "motorctl: monotonic clock failed: %s\n",
                strerror(errno));
        return -1;
    }
    deadline = now + IO_TIMEOUT_MS;

    while (offset < length) {
        now = monotonic_milliseconds();
        if (now < 0) {
            fprintf(stderr, "motorctl: monotonic clock failed: %s\n",
                    strerror(errno));
            return -1;
        }
        if (now >= deadline) {
            errno = ETIMEDOUT;
            fprintf(stderr, "motorctl: send timed out\n");
            return -1;
        }
        ssize_t count = send(fd, &data[offset], length - offset, MSG_NOSIGNAL);

        if (count > 0) {
            offset += (size_t)count;
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (wait_for_socket(fd, POLLOUT, deadline) > 0) {
                continue;
            }
            fprintf(stderr, "motorctl: send timed out or failed: %s\n",
                    strerror(errno));
            return -1;
        }
        fprintf(stderr, "motorctl: send: %s\n",
                count == 0 ? "connection closed" : strerror(errno));
        return -1;
    }
    return 0;
}

static int receive_response(int fd, char *response, size_t response_size)
{
    size_t length = 0;
    int64_t now = monotonic_milliseconds();
    int64_t deadline;

    if (response == NULL || response_size < 2) {
        errno = EINVAL;
        fprintf(stderr, "motorctl: invalid response buffer\n");
        return -1;
    }
    if (now < 0) {
        fprintf(stderr, "motorctl: monotonic clock failed: %s\n",
                strerror(errno));
        return -1;
    }
    deadline = now + IO_TIMEOUT_MS;

    while (length < response_size - 1) {
        now = monotonic_milliseconds();
        if (now < 0) {
            fprintf(stderr, "motorctl: monotonic clock failed: %s\n",
                    strerror(errno));
            return -1;
        }
        if (now >= deadline) {
            errno = ETIMEDOUT;
            fprintf(stderr, "motorctl: response timed out\n");
            return -1;
        }
        ssize_t count = recv(fd, &response[length], response_size - 1 - length,
                             0);
        char *newline;

        if (count > 0) {
            length += (size_t)count;
            response[length] = '\0';
            newline = memchr(response, '\n', length);
            if (newline != NULL) {
                newline[1] = '\0';
                return 0;
            }
            continue;
        }
        if (count == 0) {
            break;
        }
        if (errno == EINTR) {
            continue;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (wait_for_socket(fd, POLLIN, deadline) > 0) {
                continue;
            }
            fprintf(stderr, "motorctl: response timed out or failed: %s\n",
                    strerror(errno));
            return -1;
        }
        fprintf(stderr, "motorctl: receive: %s\n", strerror(errno));
        return -1;
    }

    if (length == response_size - 1) {
        fprintf(stderr, "motorctl: supervisor response is too long\n");
    } else {
        fprintf(stderr, "motorctl: incomplete supervisor response\n");
    }
    return -1;
}

static bool response_prefix(const char *response, const char *prefix)
{
    size_t length = strlen(prefix);

    return strncmp(response, prefix, length) == 0 &&
           (response[length] == ' ' || response[length] == '\n' ||
            response[length] == '\0');
}

int main(int argc, char **argv)
{
    char command[COMMAND_CAPACITY];
    char response[RESPONSE_CAPACITY];
    int command_length;
    int fd;
    FILE *output;
    int exit_status;

    command_length = build_command(argc, argv, command, sizeof(command));
    if (command_length < 0 || (size_t)command_length >= sizeof(command)) {
        usage(stderr, argv[0]);
        return 2;
    }

    if (ignore_sigpipe() != 0) {
        return 2;
    }
    fd = connect_supervisor();
    if (fd < 0) {
        return 2;
    }
    if (send_all(fd, command, (size_t)command_length) != 0) {
        close_checked(fd);
        return 2;
    }
    if (shutdown(fd, SHUT_WR) != 0) {
        fprintf(stderr, "motorctl: shutdown socket write side: %s\n",
                strerror(errno));
        close_checked(fd);
        return 2;
    }
    if (receive_response(fd, response, sizeof(response)) != 0) {
        close_checked(fd);
        return 2;
    }
    close_checked(fd);

    if (response_prefix(response, "OK") ||
        response_prefix(response, "WARN")) {
        output = stdout;
        exit_status = 0;
    } else if (response_prefix(response, "ERROR")) {
        output = stderr;
        exit_status = 1;
    } else {
        fprintf(stderr, "motorctl: malformed supervisor response: %s",
                response);
        return 2;
    }

    if (fputs(response, output) == EOF || fflush(output) == EOF) {
        fprintf(stderr, "motorctl: write output: %s\n", strerror(errno));
        return 2;
    }
    return exit_status;
}
