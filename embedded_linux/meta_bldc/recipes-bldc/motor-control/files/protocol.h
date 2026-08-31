#ifndef MOTOR_PROTOCOL_H
#define MOTOR_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#define MOTOR_SOF 0xA5u
#define MOTOR_FRAME_SIZE 8u
#define MOTOR_DATA_SIZE 4u
#ifndef MOTOR_SOCKET_PATH
#define MOTOR_SOCKET_PATH "/run/motor-supervisor.sock"
#endif
#ifndef MOTOR_LOCK_PATH
#define MOTOR_LOCK_PATH "/run/motor-supervisor.lock"
#endif

enum motor_command {
    MOTOR_CMD_GET_STATE = 0x01,
    MOTOR_CMD_SET_MODE = 0x02,
    MOTOR_CMD_ENABLE = 0x03,
    MOTOR_CMD_DISABLE = 0x04,
    MOTOR_CMD_SET_TARGET = 0x05,
    MOTOR_CMD_HEARTBEAT = 0x06,
    MOTOR_CMD_CLEAR_FAULT = 0x07,
    MOTOR_CMD_RSP_STATE = 0x81
};

enum motor_state {
    MOTOR_STATE_INIT = 0,
    MOTOR_STATE_READY = 1,
    MOTOR_STATE_RUN = 2,
    MOTOR_STATE_FAULT = 3
};

enum motor_mode {
    MOTOR_MODE_LOCAL = 0,
    MOTOR_MODE_REMOTE = 1
};

enum motor_fault {
    MOTOR_FAULT_NONE = 0,
    MOTOR_FAULT_OVERCURRENT = 1,
    MOTOR_FAULT_OVERTEMP = 2,
    MOTOR_FAULT_COMM = 3
};

struct motor_state_snapshot {
    uint8_t state;
    uint8_t mode;
    uint8_t fault;
};

struct motor_frame_parser {
    uint8_t bytes[MOTOR_FRAME_SIZE];
    size_t used;
};

uint16_t motor_crc16_ccitt(const uint8_t *data, size_t length);
void motor_frame_build(uint8_t frame[MOTOR_FRAME_SIZE], uint8_t command,
                       const uint8_t data[MOTOR_DATA_SIZE]);
int motor_frame_validate(const uint8_t frame[MOTOR_FRAME_SIZE]);
int motor_frame_decode_state(const uint8_t frame[MOTOR_FRAME_SIZE],
                             struct motor_state_snapshot *snapshot);

void motor_frame_parser_init(struct motor_frame_parser *parser);
int motor_frame_parser_push(struct motor_frame_parser *parser, uint8_t byte,
                            uint8_t frame[MOTOR_FRAME_SIZE]);

const char *motor_state_name(uint8_t state);
const char *motor_mode_name(uint8_t mode);
const char *motor_fault_name(uint8_t fault);

#endif
