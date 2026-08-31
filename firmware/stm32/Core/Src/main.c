#include "config.h"
#include "stm32f767xx.h"
#include "clock.h"
#include "GPIO.h"
#include "adc.h"
#include "timer.h"
#include "hall.h"
#include "uart.h"
#include "dac.h"

/* ====================================================================== *
 * ---------------------------------------------------------------------- *
 *  TEST_MODE 값을 바꾸고 재빌드 → 플래시 하면 각 주변장치를 단독으로
 *  검증하는 펌웨어가 됩니다. 통합 동작은 TEST_MODE_NONE (기본값).
 * ====================================================================== */
#define TEST_MODE_NONE   0   /* 통합 동작(기존 전체 로직) */
#define TEST_MODE_UART   1   /* UART2 + USART3(AT09) 송수신 테스트 — 가장 안전 */
#define TEST_MODE_ADC    2   /* ADC1/2/3 raw + 전압값 UART2 로그 */
#define TEST_MODE_HALL   3   /* Hall 센서 상태 & 속도 UART2 로그 */
#define TEST_MODE_PWM    4   /* TIM1 PWM 3상 duty sweep — 오실로스코프 확인 (모터 분리 필수) */

#define TEST_MODE        TEST_MODE_NONE

#define ENABLE_SPEED_PI  0	// PI제어기 꺼서 초기 테스트 해보기..!

/* ====================================================================== *
 *  Raspberry Pi <-> STM32 UART Protocol
 *  Fixed 8-byte frame: SOF | CMD | DATA[4] | CRC16
 * ====================================================================== */
#define UART_SOF                 0xA5U
#define UART_FRAME_SIZE          8U
#define UART_COMM_TIMEOUT_MS     500U   /* 초기 목표값. Linux Heartbeat jitter 측정 후 조정 */
#define UART_MAX_RAMP_MS         10000U

#define CMD_GET_STATE            0x01U
#define CMD_SET_MODE             0x02U
#define CMD_ENABLE               0x03U
#define CMD_DISABLE              0x04U
#define CMD_SET_TARGET           0x05U
#define CMD_HEARTBEAT            0x06U
#define CMD_CLEAR_FAULT          0x07U
#define RSP_STATE                0x81U

#define MAX3(a, b, c) (((a) > (b)) ? (((a) > (c)) ? (a) : (c)) : (((b) > (c)) ? (b) : (c)))
#define Tsamp	0.00005				// sampling time of current controller

/* TIM1 ISR execution-time measurement: PE14 */
#define ISR_DEBUG_PIN      14U
#define ISR_DEBUG_MASK     (1U << ISR_DEBUG_PIN)

void TIM1_UP_TIM10_IRQHandler(void);		/* TIM1 interrupt function(20kHz) */
void EXTI0_IRQHandler(void);
void EXTI1_IRQHandler(void);
void EXTI2_IRQHandler(void);
void SpeedCal(void);
void Update_Hall_Sequence(void);
void rampToTarget(float command, float *output, float slope);

typedef enum
{
    STATE_INIT = 0,
    STATE_READY,
    STATE_RUN,
    STATE_FAULT
} MotorState_t;

typedef enum
{
    MODE_LOCAL = 0,
    MODE_REMOTE
} ControlMode_t;

typedef enum
{
    FAULT_NONE = 0,
    FAULT_OVERCURRENT,
    FAULT_OVERTEMP,
    FAULT_COMM
} FaultCode_t;

void Motor_OutputDisable(void);
uint8_t Motor_SetMode(ControlMode_t mode);
uint8_t Motor_Enable(void);
void Motor_Disable(void);
uint8_t Motor_SetRemoteTarget(uint16_t targetPercent, uint16_t rampMs);
void Motor_EnterFault(FaultCode_t fault);
uint8_t Motor_ClearFault(void);

static uint16_t UART_CRC16_CCITT(const uint8_t *data, uint32_t length);
static void UART2_SendFrame(uint8_t cmd, const uint8_t data[4]);
static void UART_ProcessPacket(void);
static void Communication_Service(void);

float Vdc = 0.0f;
float MosfetTemp = 0.0f;
volatile float calculated_rpm =0.0f;
volatile float motor_speed_rpm = 0.0f;
float Fi, Fv, Ft=0.0f;				// digital low-pass filter coefficient
float I_Max = 0.0f;
float tempLaw =0.0f;
volatile float speed_km_h = 0.0f;
float ias_LPF, ibs_LPF, ics_LPF = 0.0f;
float OpenLoopTestRef=0.0f, OpenLoopRampOut=0.0f, ThrottleRef =0.0f, ThrottleRef_Ramp=0.0f;
float Volt = 0.0f,Throttle_ADC = 0.0f;

float ias,ibs,ics=0.0f;
float iRamp = 0.15f;
float ias_Cal,ibs_Cal,ics_Cal =0.0f;

volatile uint32_t last_hall_time = 0;
volatile uint32_t msTicks = 0;
uint32_t VoltageRef = 0;
uint32_t Tim1TestCnt =0;
uint32_t Ias_Offset,Ibs_Offset,Ics_Offset=0;
volatile uint32_t delta_time =0;
uint8_t Task_10msFlg,Task_100msFlg,Task_1sFlg,Task_500msFlg = 0;
uint8_t HA,HB,HC = 0;
uint8_t HallSum = 0;
uint8_t StartFlag = 0;
uint8_t InitCal = 0;
uint8_t FltFlg = FAULT_NONE;
uint8_t ThrottleActive = 0;
uint16_t FltCnt = 0;
uint8_t init_drive = 0;

volatile MotorState_t motorState = STATE_READY;
volatile ControlMode_t controlMode = MODE_LOCAL;
volatile float RemoteRef = 0.0f;
volatile float RemoteRampStep = 0.15f;

volatile uint8_t UartRxPacket[UART_FRAME_SIZE] = {0};
volatile uint8_t UartPacketReady = 0;
volatile uint32_t UartRxErrorCount = 0;
volatile uint32_t UartCrcErrorCount = 0;
volatile uint32_t UartRejectedPacketCount = 0;
volatile uint32_t LastHeartbeatTick = 0;

/* 500ms current-protection diagnostic window (updated only in TIM1 ISR). */
static volatile float DiagRawProtectPeakA = 0.0f;
static volatile float DiagFilteredPeakA = 0.0f;
static volatile float DiagFilteredSumA = 0.0f;
static volatile uint32_t DiagCurrentSampleCount = 0U;
static volatile uint16_t DiagMaxFltCnt = 0U;

static const float Edges_per_Revolution = HALL_EDGES_PER_REV;
extern uint32_t DutyA,DutyB,DutyC;

void Motor_OutputDisable(void)
{
    StartFlag = 0;
    ThrottleActive = 0;

    ThrottleRef = 0.0f;
    ThrottleRef_Ramp = 0.0f;
    RemoteRef = 0.0f;
    VoltageRef = 0;

    DutyA = 0;
    DutyB = 0;
    DutyC = 0;

    TIM1->CCR1 = 0;
    TIM1->CCR2 = 0;
    TIM1->CCR3 = 0;

    Disable_PWM();
}

uint8_t Motor_SetMode(ControlMode_t mode)
{
    // 운전 중에는 Mode 변경 금지
    if (motorState != STATE_READY)
    {
        return 0;
    }

    if (mode != MODE_LOCAL && mode != MODE_REMOTE)
    {
        return 0;
    }

    controlMode = mode;
    return 1;
}

uint8_t Motor_Enable(void)
{
    // READY 상태 + Fault 없음일 때만 RUN 가능
    if (motorState != STATE_READY)
    {
        return 0;
    }

    if (FltFlg != FAULT_NONE)
    {
        return 0;
    }

    ThrottleRef = 0.0f;
    ThrottleRef_Ramp = 0.0f;
    RemoteRef = 0.0f;
    VoltageRef = 0;

    StartFlag = 1;
    motorState = STATE_RUN;

    // REMOTE로 RUN 진입한 뒤 Heartbeat가 오지 않으면 timeout으로 정지
    if (controlMode == MODE_REMOTE)
    {
        LastHeartbeatTick = msTicks;
    }

    return 1;
}

void Motor_Disable(void)
{
    Motor_OutputDisable();
    motorState = STATE_READY;
}

uint8_t Motor_SetRemoteTarget(uint16_t targetPercent, uint16_t rampMs)
{
    float newTarget;
    float difference;

    // REMOTE + RUN 상태에서만 Remote Target 허용
    if (motorState != STATE_RUN || controlMode != MODE_REMOTE)
    {
        return 0;
    }

    // Target은 0~100%, Ramp Time은 0~10s 범위만 허용
    if (targetPercent > 100U || rampMs > UART_MAX_RAMP_MS)
    {
        return 0;
    }

    newTarget = ((float)targetPercent / 100.0f) * (float)(CNT_MAX - 100);

    if (rampMs == 0U)
    {
        // Ramp Time 0ms는 즉시 목표값 적용
        RemoteRampStep = (float)CNT_MAX;
    }
    else
    {
        difference = newTarget - ThrottleRef_Ramp;
        if (difference < 0.0f)
        {
            difference = -difference;
        }

        // TIM1 제어 ISR 약 10kHz = 1ms 동안 약 10회 Ramp 갱신
        RemoteRampStep = difference / ((float)rampMs * 10.0f);

        if (RemoteRampStep < 0.001f)
        {
            RemoteRampStep = 0.001f;
        }
    }

    RemoteRef = newTarget;
    return 1;
}

void Motor_EnterFault(FaultCode_t fault)
{
    Motor_OutputDisable();

    FltFlg = (uint8_t)fault;
    motorState = STATE_FAULT;
}

uint8_t Motor_ClearFault(void)
{
    if (motorState != STATE_FAULT)
    {
        return 0;
    }

    // 실제 Fault 조건이 남아 있으면 Clear 금지
    if (I_Max > OC_LEVEL)
    {
        return 0;
    }

    if (MosfetTemp > 50.0f)
    {
        return 0;
    }

    FltCnt = 0;
    FltFlg = FAULT_NONE;
    motorState = STATE_READY;

    return 1;
}

static uint16_t UART_CRC16_CCITT(const uint8_t *data, uint32_t length)
{
    uint16_t crc = 0xFFFFU;

    for (uint32_t i = 0; i < length; i++)
    {
        crc ^= (uint16_t)data[i] << 8;

        for (uint8_t bit = 0; bit < 8U; bit++)
        {
            if (crc & 0x8000U)
            {
                crc = (uint16_t)((crc << 1) ^ 0x1021U);
            }
            else
            {
                crc <<= 1;
            }
        }
    }

    return crc;
}

static void UART2_SendFrame(uint8_t cmd, const uint8_t data[4])
{
    uint8_t frame[UART_FRAME_SIZE];
    uint16_t crc;

    frame[0] = UART_SOF;
    frame[1] = cmd;
    frame[2] = data[0];
    frame[3] = data[1];
    frame[4] = data[2];
    frame[5] = data[3];

    crc = UART_CRC16_CCITT(frame, 6U);
    frame[6] = (uint8_t)(crc & 0xFFU);
    frame[7] = (uint8_t)((crc >> 8) & 0xFFU);

    for (uint8_t i = 0; i < UART_FRAME_SIZE; i++)
    {
        UART2_SendChar((char)frame[i]);
    }
}

static void UART_ProcessPacket(void)
{
    uint8_t frame[UART_FRAME_SIZE];
    uint16_t rxCrc;
    uint16_t calcCrc;
    uint8_t result = 0;

    if (UartPacketReady == 0U)
    {
        return;
    }

    // ISR이 다음 완성 Packet을 덮어쓰지 않도록 먼저 현재 Packet 복사
    for (uint8_t i = 0; i < UART_FRAME_SIZE; i++)
    {
        frame[i] = UartRxPacket[i];
    }
    UartPacketReady = 0U;

    if (frame[0] != UART_SOF)
    {
        UartRejectedPacketCount++;
        return;
    }

    rxCrc = (uint16_t)frame[6] | ((uint16_t)frame[7] << 8);
    calcCrc = UART_CRC16_CCITT(frame, 6U);

    if (rxCrc != calcCrc)
    {
        UartCrcErrorCount++;
        return;
    }

    switch (frame[1])
    {
        case CMD_GET_STATE:
        {
            uint8_t responseData[4];
            responseData[0] = (uint8_t)motorState;
            responseData[1] = (uint8_t)controlMode;
            responseData[2] = FltFlg;
            responseData[3] = 0U;
            UART2_SendFrame(RSP_STATE, responseData);
            result = 1U;
            break;
        }

        case CMD_SET_MODE:
            result = Motor_SetMode((ControlMode_t)frame[2]);
            break;

        case CMD_ENABLE:
            result = Motor_Enable();
            break;

        case CMD_DISABLE:
            if (motorState == STATE_RUN)
            {
                Motor_Disable();
                result = 1U;
            }
            break;

        case CMD_SET_TARGET:
        {
            uint16_t targetPercent = (uint16_t)frame[2] | ((uint16_t)frame[3] << 8);
            uint16_t rampMs = (uint16_t)frame[4] | ((uint16_t)frame[5] << 8);
            result = Motor_SetRemoteTarget(targetPercent, rampMs);
            break;
        }

        case CMD_HEARTBEAT:
            LastHeartbeatTick = msTicks;
            result = 1U;
            break;

        case CMD_CLEAR_FAULT:
            result = Motor_ClearFault();
            break;

        default:
            break;
    }

    if (result == 0U)
    {
        UartRejectedPacketCount++;
    }
}

static void Communication_Service(void)
{
    UART_ProcessPacket();

    // Communication Timeout은 REMOTE + RUN에서만 Motor Safety 조건으로 사용
    if (motorState == STATE_RUN && controlMode == MODE_REMOTE)
    {
        if ((uint32_t)(msTicks - LastHeartbeatTick) > UART_COMM_TIMEOUT_MS)
        {
            Motor_EnterFault(FAULT_COMM);
        }
    }
}

void USART2_IRQHandler(void)
{
    static uint8_t buildFrame[UART_FRAME_SIZE];
    static uint8_t rxIndex = 0U;
    uint32_t status = USART2->ISR;

    // UART Overrun / Framing / Noise / Parity Error 발생 시 해당 Frame 폐기 후 수신 재동기화
    if (status & (USART_ISR_ORE | USART_ISR_FE | USART_ISR_NE | USART_ISR_PE))
    {
        USART2->ICR = USART_ICR_ORECF | USART_ICR_FECF | USART_ICR_NCF | USART_ICR_PECF;
        rxIndex = 0U;
        UartRxErrorCount++;
    }

    if (USART2->ISR & USART_ISR_RXNE)
    {
        uint8_t rxByte = (uint8_t)(USART2->RDR & 0xFFU);

        // Frame 시작은 SOF(0xA5)에서만 허용
        if (rxIndex == 0U && rxByte != UART_SOF)
        {
            return;
        }

        buildFrame[rxIndex++] = rxByte;

        if (rxIndex >= UART_FRAME_SIZE)
        {
            if (UartPacketReady == 0U)
            {
                for (uint8_t i = 0; i < UART_FRAME_SIZE; i++)
                {
                    UartRxPacket[i] = buildFrame[i];
                }
                UartPacketReady = 1U;
            }
            else
            {
                // Main loop가 이전 Packet을 처리하기 전에 새 Packet이 완성되면 폐기
                UartRejectedPacketCount++;
            }

            rxIndex = 0U;
        }
    }
}

void rampToTarget(float command, float *output, float slope)
{
    // 지령 신호에 따라 출력 신호를 점진적으로 변화시킴
    if(*output < command)
    {
        *output += slope;

        if(*output > command)
        {
            *output = command;
        }
    }
    else if(*output > command)
    {
        *output -= slope;

        if(*output < command)
        {
            *output = command;
        }
    }
}

void LPF(float input, float Fx, volatile float *output)	/* digital low-pass filter */
{
  *output = (1. - Fx)*(*output) + Fx*input;
}

/* 폴트 판정 — TIM1 10kHz ISR 에서 매 tick 호출.
 * 이미 샘플된 I_Max 와 MosfetTemp 를 참고하여 FAULT 상태로 전환.
 *
 *   FltFlg 코드: 0 = 정상, 1 = 과전류, 2 = 과열, 3 = 통신
 */
static void Check_Faults(void)
{
    if (motorState == STATE_FAULT)
    {
        return;
    }

    /* 과전류 보호 — OC_LEVEL 을 연속 OC_TRIP_COUNT tick 초과하면
     * FAULT_OVERCURRENT 로 전환. */
    if (I_Max > OC_LEVEL)
    {
        if (FltCnt < OC_TRIP_COUNT)
        {
            FltCnt++;
        }

        if (FltCnt >= OC_TRIP_COUNT)
        {
            Motor_EnterFault(FAULT_OVERCURRENT);
        }
    }
    else
    {
        FltCnt = 0;
    }

    /* 과열 보호 — 80°C 이상이면 FAULT_OVERTEMP 로 전환.
     * 온도가 낮아져도 자동 RUN 하지 않고 Fault Clear 후 READY 상태로 복귀. */
    if (motorState != STATE_FAULT && MosfetTemp > 80.0f)
    {
        Motor_EnterFault(FAULT_OVERTEMP);
    }
}
float RpmRef,RpmErr,Pterm,Iterm,PIterm = 0.0f;
float Kp = 2.0f;
float Ki = 10.0f;

uint8_t SpdFlg = 0;
float RpmRef_Rmp = 0.0f;
float RpmSlope = 10.0f;
void Task_1ms(void)
{
	if(SpdFlg==1)
	{
		rampToTarget(RpmRef,&RpmRef_Rmp,RpmSlope);
	    RpmErr = RpmRef_Rmp-motor_speed_rpm;
	    Pterm = Kp*RpmErr;
	    Iterm += Ki*RpmErr*0.0005f; // 0.0005--> 속도제어기가 실행되는 주기
	    PIterm = Pterm + Iterm;

	    if(PIterm>CNT_MAX-100)
	    {
	    	PIterm = CNT_MAX-100;
	    }

	    VoltageRef = PIterm;
	}
	else
	{
		Pterm = 0.0f;
		Iterm = 0.0f;
		PIterm = 0.0f;
	}
}

/* 10ms마다 실행할 태스크 */
void Task_10ms(void)
{
    /* 통합 동작에서는 UART2를 RPi binary protocol 전용으로 사용하므로
     * 기존 ASCII Serial Grapher 로그는 UART2로 송신하지 않는다. */
    Task_10msFlg=0;
}

/* 100ms마다 실행할 태스크 */
void Task_100ms(void)
{
	Task_100msFlg=0;
}

/* 500ms마다 실행할 태스크 */
void Task_500ms(void)
{
	float rawProtectPeakA;
	float filteredPeakA;
	float filteredSumA;
	float filteredAvgA;
	float vdcSnapshot;
	float rpmSnapshot;
	float dutyPercent;
	uint32_t sampleCount;
	uint32_t voltageRefSnapshot;
	uint16_t maxFltCnt;
	MotorState_t stateSnapshot;
	uint8_t faultSnapshot;
	uint32_t primask;

	/* Keep the shared-window transaction short: snapshot and reset only. */
	primask = __get_PRIMASK();
	__disable_irq();
	rawProtectPeakA = DiagRawProtectPeakA;
	filteredPeakA = DiagFilteredPeakA;
	filteredSumA = DiagFilteredSumA;
	sampleCount = DiagCurrentSampleCount;
	maxFltCnt = DiagMaxFltCnt;
	voltageRefSnapshot = VoltageRef;
	vdcSnapshot = Vdc;
	rpmSnapshot = motor_speed_rpm;
	stateSnapshot = motorState;
	faultSnapshot = FltFlg;

	DiagRawProtectPeakA = 0.0f;
	DiagFilteredPeakA = 0.0f;
	DiagFilteredSumA = 0.0f;
	DiagCurrentSampleCount = 0U;
	DiagMaxFltCnt = 0U;
	if (primask == 0U)
	{
		__enable_irq();
	}

	filteredAvgA = (sampleCount > 0U) ? (filteredSumA / (float)sampleCount) : 0.0f;
	dutyPercent = ((float)voltageRefSnapshot * 100.0f) / (float)CNT_MAX;

	USART3_SendString("CUR,");
	UART3_SendFloat_Simple(rawProtectPeakA, 3);
	USART3_SendChar(',');
	UART3_SendFloat_Simple(filteredPeakA, 3);
	USART3_SendChar(',');
	UART3_SendFloat_Simple(filteredAvgA, 3);
	USART3_SendChar(',');
	UART3_SendFloat_Simple((float)maxFltCnt, 0);
	USART3_SendChar(',');
	UART3_SendFloat_Simple(dutyPercent, 0);
	USART3_SendChar(',');
	UART3_SendFloat_Simple(vdcSnapshot, 2);
	USART3_SendChar(',');
	UART3_SendFloat_Simple(rpmSnapshot, 0);
	USART3_SendChar(',');
	UART3_SendFloat_Simple((float)stateSnapshot, 0);
	USART3_SendChar(',');
	UART3_SendFloat_Simple((float)faultSnapshot, 0);
	USART3_SendString("\r\n");

	Task_500msFlg=0;
}

/* 1초마다 실행할 태스크 */
void Task_1sec(void)
{
	Task_1sFlg = 0;
}

/* 스케줄러 함수: msTicks 값을 기준으로 태스크 호출 */
void Scheduler(void)
{
    // 1ms 태스크는 매번 실행
    Task_1ms();

    // 10ms 주기 태스크: 1ms 카운터가 10의 배수이면 실행
    if ((msTicks % 10) == 0)
    {
    	Task_10msFlg=1;
    }
    // 100ms 주기 태스크: 1ms 카운터가 100의 배수이면 실행
    if ((msTicks % 100) == 0)
    {
    	Task_100msFlg=1;
    }
    // 500ms 주기 태스크: 1ms 카운터가 1000의 배수이면 실행
    if ((msTicks % 500) == 0)
    {
    	Task_500msFlg=1;
    }
    // 1초 주기 태스크: 1ms 카운터가 1000의 배수이면 실행
    if ((msTicks % 1000) == 0)
    {
    	Task_1sFlg=1;
    }
}

/* SysTick 인터럽트 핸들러: 1ms마다 호출됨 */
void SysTick_Handler(void)
{
    msTicks++;
    Scheduler();
}

/* SysTick 초기화 함수: 1ms 주기로 인터럽트 발생 */
void SysTick_Init(void)
{
    // SYSCLK가 216MHz일 때, 1ms마다 인터럽트가 발생하도록 설정:
    // Reload = (216,000,000 / 1000) - 1 = 215999
    SysTick->LOAD = (216000000 / 1000) - 1;
    SysTick->VAL  = 0;  // 현재 카운터 값 초기화
    // SysTick 제어: 프로세서 클록(216MHz) 사용, 인터럽트 활성, 카운터 시작
    SysTick->CTRL = SysTick_CTRL_CLKSOURCE_Msk |
                    SysTick_CTRL_TICKINT_Msk   |
                    SysTick_CTRL_ENABLE_Msk;
}

uint32_t rpmHoldCounter = 0;
float RpmNew = 0.0f;
float RpmOld = 0.0f;
void TIM1_UP_TIM10_IRQHandler(void)
{
    uint32_t adcDmaFrame;

    GPIOE->BSRR = ISR_DEBUG_MASK;    // PE14 HIGH: ISR entry

    /* CT is the DMA target being written; the other frame is complete. */
    adcDmaFrame = (DMA2_Stream4->CR & DMA_SxCR_CT) ? 0U : 1U;
    SCB_InvalidateDCache_by_Addr((uint32_t *)&AdcDmaBuffer,
                                sizeof(AdcDmaBuffer));

	Tim1TestCnt++;

    RpmNew = calculated_rpm;

    if(RpmNew==RpmOld)
    {
    	rpmHoldCounter++;
    	if(rpmHoldCounter>20000)
    	{
    		calculated_rpm = 0.0f;
    		rpmHoldCounter = 0;
    	}
    }
    RpmOld = calculated_rpm;

    if ((TIM1->SR & 0x0001) && InitCal==1)  // update interrupt flag ?
    {
    	uint32_t result = 0;

        TIM1->SR &= ~TIM_SR_UIF;

        result = AdcDmaBuffer.frame[adcDmaFrame][ADC_DMA_IAS_INDEX];
        ias_Cal=((float)(result - Ias_Offset)*ADC_VREF/ADC_FS-OFFSET_Volt)/OPAMP_GAIN;
        ias = ias_Cal;
        LPF(ias, Fi, &ias_LPF);

        result = AdcDmaBuffer.frame[adcDmaFrame][ADC_DMA_IBS_INDEX];
        ibs_Cal=((float)(result - Ibs_Offset)*ADC_VREF/ADC_FS-OFFSET_Volt)/OPAMP_GAIN;
        ibs = ibs_Cal;
        LPF(ibs, Fi, &ibs_LPF);

        result = AdcDmaBuffer.frame[adcDmaFrame][ADC_DMA_ICS_INDEX];
        ics_Cal=((float)(result - Ics_Offset)*ADC_VREF/ADC_FS-OFFSET_Volt)/OPAMP_GAIN;
        ics = ics_Cal;
        LPF(ics, Fi, &ics_LPF);

        result = AdcDmaBuffer.frame[adcDmaFrame][ADC_DMA_THROTTLE_INDEX];
        Throttle_ADC = (float)result*3.3f/4095.0f;

        result = AdcDmaBuffer.frame[adcDmaFrame][ADC_DMA_MOSFET_TEMP_INDEX];
        tempLaw = (float)result*ADC_VREF / ADC_FS;
        // NTC 3차 다항식 y = -11.48x^3 + 63.23x^2 - 149.02x + 181.97
        MosfetTemp = -11.48f*tempLaw*tempLaw*tempLaw
                   +  63.23f*tempLaw*tempLaw
                   - 149.02f*tempLaw
                   + 181.97f;

        result = AdcDmaBuffer.frame[adcDmaFrame][ADC_DMA_VDC_INDEX];
        Vdc = (float)result*(ADC_VREF/ADC_FS) / VDIV_RATIO;

        // 최대 전류 계산 후 폴트 판정 (OC + OT 를 Check_Faults() 에 위임)
        I_Max = MAX3(ias, ibs, ics);
        Check_Faults();

		/* Protection input과 같은 부호/상 선택 기준으로 500ms 진단값 누적. */
		{
			float filteredCurrent = MAX3(ias_LPF, ibs_LPF, ics_LPF);

			if (DiagCurrentSampleCount == 0U)
			{
				DiagRawProtectPeakA = I_Max;
				DiagFilteredPeakA = filteredCurrent;
			}
			else
			{
				if (I_Max > DiagRawProtectPeakA)
				{
					DiagRawProtectPeakA = I_Max;
				}
				if (filteredCurrent > DiagFilteredPeakA)
				{
					DiagFilteredPeakA = filteredCurrent;
				}
			}

			DiagFilteredSumA += filteredCurrent;
			DiagCurrentSampleCount++;
			if (FltCnt > DiagMaxFltCnt)
			{
				DiagMaxFltCnt = FltCnt;
			}
		}

        // 속도 계산
        LPF(calculated_rpm, Ft, &motor_speed_rpm);
        speed_km_h = RPM_TO_KMH(motor_speed_rpm);

        // 6-STEP 제어 모드 — 오픈루프 쓰로틀 경로
        //   SpdFlg==0: State / Mode 에 따라 LOCAL(Pot) 또는 REMOTE(UART) 지령 선택
        //   SpdFlg==1: 기존 PI 속도제어 경로 사용
        if (SpdFlg == 0)
        {
            if (motorState != STATE_RUN)
            {
                // RUN 상태가 아니면 모터 지령 제거
                Disable_PWM();
                ThrottleRef = 0.0f;
                ThrottleRef_Ramp = 0.0f;
                VoltageRef = 0;
            }
            else if (controlMode == MODE_LOCAL)
            {
                // LOCAL Mode — 기존 가변저항 제어
                if (Throttle_ADC < THROTTLE_OFF)
                {
                    ThrottleActive = 0;
                }
                else if (Throttle_ADC > THROTTLE_ON)
                {
                    ThrottleActive = 1;
                }

                if (!ThrottleActive) //히스테리시스 값 이하
                {
                    Disable_PWM();
                    ThrottleRef = 0.0f;
                    VoltageRef = 0;
                }
                else // 쓰로틀 오픈루프 구동
                {
                    ThrottleRef = Throttle_ADC * 2207.5f - 2252.2f; // 0826 듀티값 변경
                }

                rampToTarget(ThrottleRef, &ThrottleRef_Ramp, iRamp);
            }
            else
            {
                // REMOTE Mode — 가변저항은 읽더라도 Motor Command에는 사용하지 않음
                ThrottleRef = RemoteRef;
                rampToTarget(ThrottleRef, &ThrottleRef_Ramp, RemoteRampStep);
            }
        }

		if(SpdFlg == 0)
		{
			VoltageRef = (uint32_t)ThrottleRef_Ramp; // 형 변환을 통한 최종 지령 값 추출

			if(VoltageRef> CNT_MAX-100)
			{
				VoltageRef = CNT_MAX-100;
			}
		}

        if(FltFlg == 0 && InitCal == 1)
		{
			Update_Switching_Pattern(HallSum);
		}
		else
		{
			VoltageRef = 0;
			DutyA = 0;
			DutyB = 0;
			DutyC = 0;
			// PWM 완전 차단
			TIM1->CCR1 = 0;
			TIM1->CCR2 = 0;
			TIM1->CCR3 = 0;
		}
    }
    GPIOE->BSRR = (ISR_DEBUG_MASK << 16U);   // PE14 LOW: ISR exit
}
void EXTI0_IRQHandler(void) //PORTD0 HA
{
    if (EXTI->PR & EXTI_PR_PR0)
    {
        EXTI->PR |= EXTI_PR_PR0; // 인터럽트 플래그 클리어
        Update_Hall_Sequence();
        SpeedCal();
    }
}

void EXTI1_IRQHandler(void) //PORTD1 HB
{
    if (EXTI->PR & EXTI_PR_PR1)
    {
        EXTI->PR |= EXTI_PR_PR1; // 인터럽트 플래그 클리어
        Update_Hall_Sequence();
        SpeedCal();
    }
}

void EXTI2_IRQHandler(void) //PORTD2 HC
{
	if (EXTI->PR & EXTI_PR_PR2)
	{
        EXTI->PR |= EXTI_PR_PR2; // 인터럽트 플래그 클리어
        Update_Hall_Sequence();
        SpeedCal();
    }
}
uint32_t ia_raw, ib_raw, ic_raw, th_raw, vdc_raw, temp_raw = 0;
int main(void)
{
	Initialize_MCU();
	SysTick_Init();    /* 모든 TEST_MODE 공통 — Delay_ms(clock.c) 동작을 위해 필수 */

/* =================== 모듈 Bring-up 테스트 분기 ======================= */
#if   TEST_MODE == TEST_MODE_UART
	/* ---------------- UART bring-up ----------------
	 * 확인: UART2 USB-TTL (115200bps) + USART3 AT09 블루투스 페어링 후 앱.
	 *       500ms 마다 "UART2 OK" / "BT3  OK" + 카운터 증가값 수신.
	 */
	UART2_Init();
	AT09_Init();
	uint32_t cnt = 0;
	while(1)
	{
		/* Serial Grapher 파서 친화: "key:value,key:value" 포맷.
		 * cnt 는 단조 증가 카운터 → 그래프 위에서 직선 기울기로 관측. */
		UART2_SendString("cnt:");
		UART2_SendFloat_Simple((float)cnt, 0);
		UART2_SendString("\r\n");

		USART3_SendString("cnt:");
		UART3_SendFloat_Simple((float)cnt, 0);
		USART3_SendString("\r\n");

		cnt++;
		Delay_ms(10);
	}

#elif TEST_MODE == TEST_MODE_ADC
	/* ---------------- ADC bring-up ----------------
	 * 확인: UART2(PD5/PD6, 115200bps) 터미널에 매 200ms 출력.
	 *       PA0/PA1/PA2 (상 전류) raw count, PA7(쓰로틀)/PA3(Vdc)/PA6(MosfetTemp) 환산값.
	 */
	Initialize_ADC();
	UART2_Init();
	while(1)
	{
		/* PA0 - ADC1 ch0 (Ias) */
		ADC1->SQR3 = 0x00000000;
		ADC1->CR2 |= 0x40000000;
		while(!(ADC1->SR & 0x00000002));
		ia_raw = ADC1->DR;

		/* PA7 - ADC2 ch7 (Throttle) */
		ADC2->SQR3 = 0x00000007;
		ADC2->CR2 |= 0x40000000;
		while(!(ADC2->SR & 0x00000002));
		th_raw = ADC2->DR;

		/* PA3 - ADC3 ch3 (Vdc, 저항분압 후 입력 — VDIV_RATIO 로 원전압 환산) */
		ADC3->SQR3 = 0x00000003;
		ADC3->CR2 |= 0x40000000;
		while(!(ADC3->SR & 0x00000002));
		vdc_raw = ADC3->DR;

		/* PA6 - ADC1 ch6 (MosfetTemp, NTC → 3차 다항식 환산) */
		ADC1->SQR3 = 0x00000006;
		ADC1->CR2 |= 0x40000000;
		while(!(ADC1->SR & 0x00000002));
		temp_raw = ADC1->DR;

		float v_temp = (float)temp_raw * ADC_VREF / ADC_FS;
		float t_degC = -11.48f*v_temp*v_temp*v_temp
		             +  63.23f*v_temp*v_temp
		             - 149.02f*v_temp
		             + 181.97f;

		/* Serial Grapher 사용: "IA:raw,IB:raw,IC:raw,TH:V,Vdc:V,Temp:C"
		 * 단위 기호(V/C) 는 제거 — 파서가 값만 추출해야 함. 한 라인 내 전체 필드
		 * 를 콤마로 연결하고 끝에만 "\r\n" — 그래야 파서가 한 샘플로 취급. */
		UART2_SendString("IA:");   UART2_SendFloat_Simple((float)ia_raw, 0);
		UART2_SendString(",TH:");  UART2_SendFloat_Simple((float)th_raw * ADC_VREF / ADC_FS, 3);
		UART2_SendString(",Vdc:"); UART2_SendFloat_Simple((float)vdc_raw * (ADC_VREF/ADC_FS) / VDIV_RATIO, 2);
		UART2_SendString(",Temp:"); UART2_SendFloat_Simple(t_degC, 1);
		UART2_SendString("\r\n");

		Delay_ms(10);
	}

#elif TEST_MODE == TEST_MODE_HALL
	/* ---------------- Hall sensor bring-up ----------------
	 * 확인: 바퀴를 손으로 돌리면 HallSum 이 {1,3,2,6,4,5} 순환.
	 *       100ms 주기로 UART2 에 상태 출력.
	 */
	Initialize_Hall_Sensors();   /* EXTI0/1/2 enable */
	Initialize_TIM2();            /* SpeedCal 용 타이머 */
	UART2_Init();
	while(1)
	{
		HA = GPIOD->IDR & GPIO_IDR_ID0;
		HB = (GPIOD->IDR & GPIO_IDR_ID1) >> 1;
		HC = (GPIOD->IDR & GPIO_IDR_ID2) >> 2;
		HallSum = HA*4 + HB*2 + HC;

		/* Serial Grapher 사용: 각 필드를 ':' 로 값 구분, ',' 로 필드 구분.
		 * HA/HB/HC/SUM 은 0~7 정수를 float 로 캐스팅해서 SendFloat_Simple 이 처리.
		 * 그래프에서 HallSum 의 6-step 순환(1→3→2→6→4→5→1) 이 계단 파형으로 보임. */
		UART2_SendString("HA:");    UART2_SendFloat_Simple((float)HA, 0);
		UART2_SendString(",HB:");   UART2_SendFloat_Simple((float)HB, 0);
		UART2_SendString(",HC:");   UART2_SendFloat_Simple((float)HC, 0);
		UART2_SendString(",SUM:");  UART2_SendFloat_Simple((float)HallSum, 0);
		UART2_SendString("\r\n");

		Delay_ms(100);
	}

#elif TEST_MODE == TEST_MODE_PWM
	/* ---------------- PWM bring-up ----------------
	 * 안전: 모터 분리.
	 * 확인: PE8/PE10/PE12 에 20kHz 센터얼라인 PWM, CCR 값에 따라
	 *       듀티가 ~90% → ~50% → ~10% → ~50% → ~90% 로 스윕.
	 *       상보 PE9/PE11/PE13 + 1us deadtime.
	 *
	 * 구현 메모: Initialize_PWM() 이 TIM1 ARR/CCMR/CCER/BDTR 을 모두 설정하고
	 *           MOE=1 까지 켬 → CCR 레지스터만 바꾸면 즉시 파형 변화.
	 *           Set_Phases() 는 모터 제어 상위 API(VoltageRef/StartFlag/phase
	 *           flag 의존)이므로 순수 PWM bring-up 단계에서는 CCR 직접 접근.
	 */
	Initialize_PWM();
	while (1)
	{
		/* CNT_MAX = 5400. PWM 모드 2 + 센터얼라인:
		 * 작은 CCR → 출력 활성 시간 큼(고듀티), 큰 CCR → 저듀티. */
		for (uint32_t ccr = 500; ccr <= 4900; ccr += 200)
		{
			TIM1->CCR1 = ccr;
			TIM1->CCR2 = ccr;
			TIM1->CCR3 = ccr;
			Delay_ms(100);
		}
		for (uint32_t ccr = 4900; ccr >= 500; ccr -= 200)
		{
			TIM1->CCR1 = ccr;
			TIM1->CCR2 = ccr;
			TIM1->CCR3 = ccr;
			Delay_ms(100);
		}
	}

#else   /* ===================== TEST_MODE_NONE : 기존 통합 동작 ===================== */
	Initialize_ADC();				// initialize ADC for measurement
	Initialize_PWM();				// initialize TIM1 for PWM

    /* PE14: TIM1 ISR timing measurement output */
    GPIOE->MODER &= ~(3U << (ISR_DEBUG_PIN * 2U));
    GPIOE->MODER |=  (1U << (ISR_DEBUG_PIN * 2U));   // General-purpose output

    GPIOE->OTYPER &= ~ISR_DEBUG_MASK;                // Push-pull

    GPIOE->OSPEEDR &= ~(3U << (ISR_DEBUG_PIN * 2U));
    GPIOE->OSPEEDR |=  (3U << (ISR_DEBUG_PIN * 2U)); // Very high speed

    GPIOE->PUPDR &= ~(3U << (ISR_DEBUG_PIN * 2U));   // No pull

    GPIOE->BSRR = (ISR_DEBUG_MASK << 16U);           // Initial LOW

    Initialize_TIM2();
	FLT_LED_Init();
	Initialize_Hall_Sensors();
	AT09_Init();
	USART3_SendString("TYPE,RAW_PK_A,FILT_PK_A,FILT_AVG_A,MAX_FLTCNT,DUTY,VDC,RPM,STATE,FAULT\r\n");
	/* SysTick_Init() 은 main() 진입 직후에 이미 호출됨 */
	DAC_Init();
	UART2_Init();

	Fi = 2.*PI*500.*Tsamp/(1.+2.*PI*500.*Tsamp);	// fci = 500[Hz] for phase current
	Ft = 2.*PI*1.*Tsamp/(1.+2.*PI*1.*Tsamp);	// fct = 100[Hz] for IPM temperature

	for(int i = 0; i < 10; i++)
	{
	    // PA0(ias) 읽기 - ADC1
	    ADC1->SQR3 = 0x00000000;  // SQ1=0 (채널 0)
	    ADC1->CR2 |= 0x40000000;
	    while(!(ADC1->SR & 0x00000002));
	    Ias_Offset += ADC1->DR;

	    // PA1(ibs) 읽기 - ADC2
	    ADC2->SQR3 = 0x00000001;  // SQ1=1 (채널 1)
	    ADC2->CR2 |= 0x40000000;
	    while(!(ADC2->SR & 0x00000002));
	    Ibs_Offset += ADC2->DR;

	    // PA2(ics) 읽기 - ADC3
	    ADC3->SQR3 = 0x00000002;  // SQ1=2 (채널 2)
	    ADC3->CR2 |= 0x40000000;
	    while(!(ADC3->SR & 0x00000002));
	    Ics_Offset += ADC3->DR;
	}

	// 평균 오프셋 계산
	Ias_Offset = (Ias_Offset/10) - 2048;
	Ibs_Offset = (Ibs_Offset/10) - 2048;
	Ics_Offset = (Ics_Offset/10) - 2048;

	while(!(TIM1->CR1 & 0x0010));                 // TIM1 underflow event ?
	TIM1->RCR = 0x0001;                           // 50 us period update(RCR = 1)
	Start_ADC_DMA();

	if(FltFlg != FAULT_NONE)
	{
		InitCal = 0;
	}
	else
	{
		InitCal = 1;
	}

	TIM1->SR &= ~TIM_SR_UIF;
	NVIC->ISER[0] |= 0x02000000;			// enable (25)TIM1 update Interrupt

	// 모터가 정지되어 있는 상태에서는 엣지 검출을 통한 홀센서 신호를 검출할 수 없으므로 초기 위치 필요
	HA = GPIOD->IDR & GPIO_IDR_ID0;
	HB = (GPIOD->IDR & GPIO_IDR_ID1) >> 1;
	HC = (GPIOD->IDR & GPIO_IDR_ID2) >> 2;
	HallSum = HA*2*2+HB*2*1+HC;

#if ENABLE_SPEED_PI == 1
	SpdFlg = 1;
#endif

    /* 최종 운전 시작 상태: READY / LOCAL.
     * 실제 RUN 진입은 UART CMD_ENABLE을 통해 명시적으로 수행한다. */
    Motor_OutputDisable();
    FltFlg = FAULT_NONE;
    motorState = STATE_READY;
    controlMode = MODE_LOCAL;
    LastHeartbeatTick = msTicks;

  while(1)
    {
      // UART Packet 처리 + REMOTE Communication Timeout 감시
      Communication_Service();
	  if(Task_1sFlg==1)
	  {
		  Task_1sec();
	  }
	  else if(Task_10msFlg==1)
	  {
		  Task_10ms();
	  }
	  else if(Task_100msFlg==1)
	  {
		  Task_100ms();
	  }
	  else if(Task_500msFlg==1)
	  {
		  Task_500ms();
	  }
	  else
	  {

	  }
	  if(StartFlag==1 && motorState == STATE_RUN)//StartFlag와 RUN 상태를 모두 만족할 때만 PWM Enable
	  {
		  Enable_PWM();
		  if(init_drive==0)
		  {
			 init_drive=1;
		  }
	  }
	  else
	  {
		  Disable_PWM();
		  init_drive=0;
	  }

    }
//while End
#endif  /* TEST_MODE 분기 종료 */
  }

void SpeedCal(void)
{
    // 현재 타이머 값 읽기
    volatile uint32_t current_time = TIM2->CNT;

    // 타이머 오버플로우 처리
    if (current_time >= last_hall_time)
    {
        delta_time = current_time - last_hall_time;
    }
    else
    {
        // 오버플로우 발생: 타이머가 ARR 값에 도달하여 리셋됨
        delta_time = (TIM2->ARR - last_hall_time) + current_time + 1;
    }

    last_hall_time = current_time;

    // delta_time이 0이 아닐 때만 RPM 계산
        // 가정: 한 회전당 6개의 홀 센서 이벤트 발생 (6 edges per revolution)
        // Timer 주파수: 54MHz
        // RPM 계산 공식: RPM = (60 * Clock_Frequency) / (Edges_per_Revolution * delta_time)
    if(delta_time <= 500)
    {
    	//너무 빠른 신호는 무시
    }
    else
    {
    	calculated_rpm = (60.0f * 54000000.0f) / ((Edges_per_Revolution * (float)delta_time));
    }
}

void Update_Hall_Sequence(void)
{
    // PD0, PD1, PD2에서 홀 신호 읽기
    HA = GPIOD->IDR & GPIO_IDR_ID0;
    HB = (GPIOD->IDR & GPIO_IDR_ID1) >> 1;
    HC = (GPIOD->IDR & GPIO_IDR_ID2) >> 2;
	HallSum = HA*2*2+HB*2*1+HC;
}
