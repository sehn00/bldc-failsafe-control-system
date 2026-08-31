#include "protocol.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned int failures;

#define CHECK(condition)                                                       \
    do {                                                                       \
        if (!(condition)) {                                                    \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__,          \
                    #condition);                                               \
            ++failures;                                                        \
        }                                                                      \
    } while (0)


static void test_crc_vectors(void)
{
    static const uint8_t prefixes[][6] = {
        {0xA5, 0x02, 0x01, 0x00, 0x00, 0x00},
        {0xA5, 0x03, 0x00, 0x00, 0x00, 0x00},
        {0xA5, 0x06, 0x00, 0x00, 0x00, 0x00}
    };
    static const uint16_t expected[] = {0x9E0E, 0x42EB, 0x61BC};
    size_t i;

    for (i = 0; i < sizeof(expected) / sizeof(expected[0]); ++i) {
        CHECK(motor_crc16_ccitt(prefixes[i], sizeof(prefixes[i])) ==
              expected[i]);
    }
}

static void test_frame_build_and_validation(void)
{
    static const uint8_t expected_get_state[MOTOR_FRAME_SIZE] = {
        0xA5, 0x01, 0x00, 0x00, 0x00, 0x00, 0x68, 0x06
    };
    static const uint8_t expected[MOTOR_FRAME_SIZE] = {
        0xA5, 0x02, 0x01, 0x00, 0x00, 0x00, 0x0E, 0x9E
    };
    uint8_t data[MOTOR_DATA_SIZE] = {1, 0, 0, 0};
    uint8_t frame[MOTOR_FRAME_SIZE];

    motor_frame_build(NULL, MOTOR_CMD_GET_STATE, NULL);
    motor_frame_build(frame, MOTOR_CMD_GET_STATE, NULL);
    CHECK(memcmp(frame, expected_get_state, sizeof(frame)) == 0);

    motor_frame_build(frame, MOTOR_CMD_SET_MODE, data);
    CHECK(memcmp(frame, expected, sizeof(frame)) == 0);
    CHECK(motor_frame_validate(frame));
    frame[3] ^= 0x01u;
    CHECK(!motor_frame_validate(frame));
}

static void test_target_encoding(void)
{
    uint8_t data[MOTOR_DATA_SIZE];
    uint8_t frame[MOTOR_FRAME_SIZE];
    const unsigned int target = 100;
    const unsigned int ramp = 10000;

    data[0] = (uint8_t)(target & 0xFFu);
    data[1] = (uint8_t)(target >> 8);
    data[2] = (uint8_t)(ramp & 0xFFu);
    data[3] = (uint8_t)(ramp >> 8);
    motor_frame_build(frame, MOTOR_CMD_SET_TARGET, data);

    CHECK(frame[2] == 0x64);
    CHECK(frame[3] == 0x00);
    CHECK(frame[4] == 0x10);
    CHECK(frame[5] == 0x27);
    CHECK(motor_frame_validate(frame));
}

static void test_state_decode(void)
{
    static const uint8_t known_response[MOTOR_FRAME_SIZE] = {
        0xA5, 0x81, 0x01, 0x00, 0x00, 0x00, 0x0C, 0x52
    };
    uint8_t data[MOTOR_DATA_SIZE] = {
        MOTOR_STATE_READY, MOTOR_MODE_REMOTE, MOTOR_FAULT_NONE, 0x7F
    };
    uint8_t frame[MOTOR_FRAME_SIZE];
    struct motor_state_snapshot snapshot;

    CHECK(motor_frame_decode_state(known_response, &snapshot) == 0);
    CHECK(snapshot.state == MOTOR_STATE_READY);
    CHECK(snapshot.mode == MOTOR_MODE_LOCAL);
    CHECK(snapshot.fault == MOTOR_FAULT_NONE);

    motor_frame_build(frame, MOTOR_CMD_RSP_STATE, data);
    CHECK(motor_frame_decode_state(frame, &snapshot) == 0);
    CHECK(snapshot.state == MOTOR_STATE_READY);
    CHECK(snapshot.mode == MOTOR_MODE_REMOTE);
    CHECK(snapshot.fault == MOTOR_FAULT_NONE);

    data[0] = 4;
    motor_frame_build(frame, MOTOR_CMD_RSP_STATE, data);
    CHECK(motor_frame_decode_state(frame, &snapshot) != 0);

    data[0] = MOTOR_STATE_READY;
    data[1] = 2;
    motor_frame_build(frame, MOTOR_CMD_RSP_STATE, data);
    CHECK(motor_frame_decode_state(frame, &snapshot) != 0);

    data[1] = MOTOR_MODE_REMOTE;
    data[2] = 4;
    motor_frame_build(frame, MOTOR_CMD_RSP_STATE, data);
    CHECK(motor_frame_decode_state(frame, &snapshot) != 0);

    data[2] = MOTOR_FAULT_NONE;
    motor_frame_build(frame, MOTOR_CMD_GET_STATE, data);
    CHECK(motor_frame_decode_state(frame, &snapshot) != 0);
}

static void feed(struct motor_frame_parser *parser, const uint8_t *bytes,
                 size_t length, unsigned int *frames)
{
    uint8_t frame[MOTOR_FRAME_SIZE];
    size_t i;

    for (i = 0; i < length; ++i) {
        if (motor_frame_parser_push(parser, bytes[i], frame) == 1) {
            CHECK(motor_frame_validate(frame));
            ++*frames;
        }
    }
}

static void test_stream_parser(void)
{
    struct motor_frame_parser parser;
    uint8_t data[MOTOR_DATA_SIZE] = {
        MOTOR_STATE_RUN, MOTOR_MODE_REMOTE, MOTOR_FAULT_NONE, 0
    };
    uint8_t good[MOTOR_FRAME_SIZE];
    uint8_t bad[MOTOR_FRAME_SIZE];
    uint8_t noise[] = {0x00, 0x55, 0xA4};
    uint8_t embedded_prefix[] = {MOTOR_SOF, 0x00, 0x00};
    unsigned int frames = 0;

    motor_frame_build(good, MOTOR_CMD_RSP_STATE, data);
    memcpy(bad, good, sizeof(bad));
    bad[2] ^= 0x10u;

    motor_frame_parser_init(&parser);
    feed(&parser, noise, sizeof(noise), &frames);
    feed(&parser, good, 3, &frames);
    feed(&parser, &good[3], MOTOR_FRAME_SIZE - 3, &frames);
    CHECK(frames == 1);

    feed(&parser, bad, sizeof(bad), &frames);
    feed(&parser, good, sizeof(good), &frames);
    CHECK(frames == 2);

    feed(&parser, good, sizeof(good), &frames);
    feed(&parser, good, sizeof(good), &frames);
    CHECK(frames == 4);

    motor_frame_parser_init(&parser);
    frames = 0;
    feed(&parser, embedded_prefix, sizeof(embedded_prefix), &frames);
    feed(&parser, good, sizeof(good), &frames);
    CHECK(frames == 1);
}

static void test_defensive_validation(void)
{
    struct motor_frame_parser parser;
    struct motor_state_snapshot snapshot;
    uint8_t frame[MOTOR_FRAME_SIZE];

    motor_frame_build(frame, MOTOR_CMD_GET_STATE, NULL);
    CHECK(motor_crc16_ccitt(NULL, 1) == 0);
    CHECK(!motor_frame_validate(NULL));
    CHECK(motor_frame_decode_state(NULL, &snapshot) != 0);
    CHECK(motor_frame_decode_state(frame, NULL) != 0);
    motor_frame_parser_init(NULL);
    CHECK(motor_frame_parser_push(NULL, MOTOR_SOF, frame) < 0);
    motor_frame_parser_init(&parser);
    CHECK(motor_frame_parser_push(&parser, MOTOR_SOF, NULL) < 0);
    parser.used = MOTOR_FRAME_SIZE;
    CHECK(motor_frame_parser_push(&parser, MOTOR_SOF, frame) < 0);
    CHECK(parser.used == 0);
}

int main(void)
{
    test_crc_vectors();
    test_frame_build_and_validation();
    test_target_encoding();
    test_state_decode();
    test_stream_parser();
    test_defensive_validation();

    if (failures != 0) {
        fprintf(stderr, "%u protocol test(s) failed\n", failures);
        return EXIT_FAILURE;
    }

    puts("protocol tests passed");
    return EXIT_SUCCESS;
}
