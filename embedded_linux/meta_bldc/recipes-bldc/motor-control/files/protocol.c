#include "protocol.h"

#include <string.h>

uint16_t motor_crc16_ccitt(const uint8_t *data, size_t length)
{
    uint16_t crc = 0xFFFFu;
    size_t i;

    if (data == NULL && length != 0) {
        return 0;
    }
    for (i = 0; i < length; ++i) {
        unsigned int bit;

        crc ^= (uint16_t)data[i] << 8;
        for (bit = 0; bit < 8; ++bit) {
            if ((crc & 0x8000u) != 0u) {
                crc = (uint16_t)((crc << 1) ^ 0x1021u);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
    }

    return crc;
}

void motor_frame_build(uint8_t frame[MOTOR_FRAME_SIZE], uint8_t command,
                       const uint8_t data[MOTOR_DATA_SIZE])
{
    uint16_t crc;

    if (frame == NULL) {
        return;
    }
    frame[0] = MOTOR_SOF;
    frame[1] = command;
    if (data != NULL) {
        memcpy(&frame[2], data, MOTOR_DATA_SIZE);
    } else {
        memset(&frame[2], 0, MOTOR_DATA_SIZE);
    }

    crc = motor_crc16_ccitt(frame, 6);
    frame[6] = (uint8_t)(crc & 0xFFu);
    frame[7] = (uint8_t)(crc >> 8);
}


int motor_frame_validate(const uint8_t frame[MOTOR_FRAME_SIZE])
{
    uint16_t expected;
    uint16_t received;

    if (frame == NULL || frame[0] != MOTOR_SOF) {
        return 0;
    }

    expected = motor_crc16_ccitt(frame, 6);
    received = (uint16_t)frame[6] | ((uint16_t)frame[7] << 8);
    return expected == received;
}

int motor_frame_decode_state(const uint8_t frame[MOTOR_FRAME_SIZE],
                             struct motor_state_snapshot *snapshot)
{
    if (frame == NULL || snapshot == NULL || !motor_frame_validate(frame) ||
        frame[1] != MOTOR_CMD_RSP_STATE) {
        return -1;
    }

    if (frame[2] > MOTOR_STATE_FAULT || frame[3] > MOTOR_MODE_REMOTE ||
        frame[4] > MOTOR_FAULT_COMM) {
        return -1;
    }

    snapshot->state = frame[2];
    snapshot->mode = frame[3];
    snapshot->fault = frame[4];
    return 0;
}

void motor_frame_parser_init(struct motor_frame_parser *parser)
{
    if (parser != NULL) {
        parser->used = 0;
    }
}

int motor_frame_parser_push(struct motor_frame_parser *parser, uint8_t byte,
                            uint8_t frame[MOTOR_FRAME_SIZE])
{
    size_t next_sof;

    if (parser == NULL || frame == NULL) {
        return -1;
    }
    if (parser->used >= MOTOR_FRAME_SIZE) {
        parser->used = 0;
        return -1;
    }
    if (parser->used == 0) {
        if (byte != MOTOR_SOF) {
            return 0;
        }
        parser->bytes[parser->used++] = byte;
        return 0;
    }

    parser->bytes[parser->used++] = byte;
    if (parser->used < MOTOR_FRAME_SIZE) {
        return 0;
    }

    if (motor_frame_validate(parser->bytes)) {
        memcpy(frame, parser->bytes, MOTOR_FRAME_SIZE);
        parser->used = 0;
        return 1;
    }

    next_sof = 1;
    while (next_sof < MOTOR_FRAME_SIZE &&
           parser->bytes[next_sof] != MOTOR_SOF) {
        ++next_sof;
    }

    if (next_sof < MOTOR_FRAME_SIZE) {
        parser->used = MOTOR_FRAME_SIZE - next_sof;
        memmove(parser->bytes, &parser->bytes[next_sof], parser->used);
    } else {
        parser->used = 0;
    }

    return -1;
}

const char *motor_state_name(uint8_t state)
{
    static const char *const names[] = {"INIT", "READY", "RUN", "FAULT"};

    return state <= MOTOR_STATE_FAULT ? names[state] : "UNKNOWN";
}

const char *motor_mode_name(uint8_t mode)
{
    static const char *const names[] = {"LOCAL", "REMOTE"};

    return mode <= MOTOR_MODE_REMOTE ? names[mode] : "UNKNOWN";
}

const char *motor_fault_name(uint8_t fault)
{
    static const char *const names[] = {
        "NONE", "OVERCURRENT", "OVERTEMP", "COMM"
    };

    return fault <= MOTOR_FAULT_COMM ? names[fault] : "UNKNOWN";
}
