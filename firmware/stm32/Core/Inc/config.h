/*
 * config.h
 *
 *  Created on: __Aug__ 21, 2025
 *      Author:
 */

#ifndef INC_CONFIG_H_
#define INC_CONFIG_H_

#define MOTOR_KIT_A   1
#define MOTOR_KIT_B   2

#define MOTOR_KIT     MOTOR_KIT_B   /* ← MOTOR_KIT_A 또는 MOTOR_KIT_B */

/* --- A키트 전용: 모터 종류 선택 (B키트에서는 무시됨) --- */
#define MOTOR_TYPE_SMALL     1   /* 소형 BLDC/PMSM */
#define MOTOR_TYPE_INWHEEL   2   /* 인휠모터 */

#define MOTOR_TYPE    MOTOR_TYPE_SMALL  /* ← MOTOR_TYPE_SMALL 또는 MOTOR_TYPE_INWHEEL */

#if   MOTOR_KIT == MOTOR_KIT_A
    #if   MOTOR_TYPE == MOTOR_TYPE_SMALL
        #define USE_INWHEEL        0
        #define HALL_EDGES_PER_REV 24.0f
        #define OC_LEVEL           5.0f     /* 소형모터 허용 최대 전류 [A] */
    #elif MOTOR_TYPE == MOTOR_TYPE_INWHEEL
        #define USE_INWHEEL        1
        #define HALL_EDGES_PER_REV 90.0f
        #define OC_LEVEL           35.0f    /* 인휠모터 허용 최대 전류 [A] */
    #else
        #error "MOTOR_TYPE must be MOTOR_TYPE_SMALL or MOTOR_TYPE_INWHEEL"
    #endif
    #define OPAMP_GAIN         0.044f   /* A키트 전류센싱 게인 */
#elif MOTOR_KIT == MOTOR_KIT_B
    #define USE_INWHEEL        0
    #define HALL_EDGES_PER_REV 24.0f
    #define OPAMP_GAIN         0.15f    /* 10mΩ 션트 × 15배 차동증폭 */
    #define OC_LEVEL           5.0f     /* B키트 허용 최대 전류 [A] */
#else
    #error "MOTOR_KIT must be MOTOR_KIT_A or MOTOR_KIT_B"
#endif

// ---------- Math ----------
#ifndef PI
#define PI 3.14159f
#endif

// ---------- Mechanics ----------
// 바퀴 지름 [m] — 하드웨어에 맞게 수정
#ifndef WHEEL_DIAMETER_M
#define WHEEL_DIAMETER_M 0.2000f // 예: 8-inch ≈ 0.2032 m
#endif
#define WHEEL_CIRCUM_M (PI * (WHEEL_DIAMETER_M))

// 1: 시계방향(CW), 0: 반시계(CCW) — 기계적 회전 방향 (키트와 독립)
#ifndef MOTOR_DIR_CW
#define MOTOR_DIR_CW 1
#define MOTOR_DIR_CCW 0
#endif

#ifndef MOTOR_DIR
#define MOTOR_DIR MOTOR_DIR_CW
#endif

// ---------- ADC & Sensors ----------
#define ADC_VREF 3.3f
#define ADC_FS 4095.0f
#define VDIV_RATIO 0.057362f // __Vdc__ 측정 저항분배 비율
#define OFFSET_Volt 1.65f

// ---------- Thresholds ----------
#define THROTTLE_OFF 1.00f
#define THROTTLE_ON 1.1f

#define OC_TRIP_COUNT 1000U // 50ms @ 20kHz TIM1 ISR
#define RPM_TO_KMH(rpm) ((rpm) * (WHEEL_CIRCUM_M) * 60.0f / 1000.0f)
#endif /* INC_CONFIG_H_ */
