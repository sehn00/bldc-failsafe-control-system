
#include "config.h"
#include "hall.h"
#include "timer.h"
extern uint32_t VoltageRef;
uint32_t DutyA =0;
uint32_t DutyB =0;
uint32_t DutyC =0;
extern uint8_t StartFlag;

void Initialize_Hall_Sensors(void)
{
    // 1) GPIOD 클록 활성화
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIODEN;

    // 2) PD0, PD1, PD2를 입력 모드로 설정
    // MODER 레지스터에서 해당 핀의 비트를 00(입력)으로
    GPIOD->MODER &= ~((3 << (0 * 2)) |   // PD0
                      (3 << (1 * 2)) |   // PD1
                      (3 << (2 * 2)));   // PD2
    RCC->APB2ENR |= RCC_APB2ENR_SYSCFGEN;  // SYSCFG 클록 인가

     // EXTI0~2를 PD0~2로 매핑
     SYSCFG->EXTICR[0] &= ~(
         SYSCFG_EXTICR1_EXTI0_Msk |
         SYSCFG_EXTICR1_EXTI1_Msk |
         SYSCFG_EXTICR1_EXTI2_Msk
     );
     SYSCFG->EXTICR[0] |= (
         SYSCFG_EXTICR1_EXTI0_PD |
         SYSCFG_EXTICR1_EXTI1_PD |
         SYSCFG_EXTICR1_EXTI2_PD
     );

     // EXTI 라인 설정: IMR, RTSR, FTSR
     EXTI->IMR |= (1 << 0) | (1 << 1) | (1 << 2); // EXTI0, EXTI1, EXTI2 마스크 해제
     EXTI->RTSR |= (1 << 0) | (1 << 1) | (1 << 2); // 상승 엣지 트리거
     EXTI->FTSR |= (1 << 0) | (1 << 1) | (1 << 2); // 하강 엣지 트리거

     // NVIC에서 EXTI0, EXTI1, EXTI2 IRQ 활성화
     NVIC_EnableIRQ(EXTI0_IRQn);
     NVIC_EnableIRQ(EXTI1_IRQn);
     NVIC_EnableIRQ(EXTI2_IRQn);
}

void phases_fwd(uint8_t HallSum)
{
	#if (USE_INWHEEL==1)
		// 인휠모터 매핑
		switch(HallSum){
			case 5: Set_Phases( 0, -1, 1); break;
			case 3: Set_Phases(-1, 1, 0); break;
			case 1: Set_Phases(-1, 0, 1); break;
			case 6: Set_Phases( 1, 0, -1); break;
			case 4: Set_Phases( 1, -1, 0); break;
			case 2: Set_Phases( 0, 1, -1); break;
			default: Set_Phases(0,0,0); break;
		}
	#else
		// 소형 BLDC 매핑
		switch(HallSum){
			case 6: Set_Phases( 0, -1, 1); break;
			case 4: Set_Phases(-1, 0, 1); break;
			case 5: Set_Phases(-1, 1, 0); break;
			case 1: Set_Phases( 0, 1, -1); break;
			case 3: Set_Phases( 1, 0, -1); break;
			case 2: Set_Phases( 1, -1, 0); break;
			default: Set_Phases(0,0,0); break;
		}
	#endif
}

void phases_rev(uint8_t HallSum)
{
	#if (USE_INWHEEL==1)
		// 인휠모터 매핑
		switch(HallSum){
		case 5: Set_Phases( 0, 1, -1); break;
		case 3: Set_Phases( 1, -1, 0); break;
		case 1: Set_Phases( 1, 0, -1); break;
		case 6: Set_Phases(-1, 0, 1); break;
		case 4: Set_Phases(-1, 1, 0); break;
		case 2: Set_Phases( 0, -1, 1); break;
		default: Set_Phases(0,0,0); break;
		}
	#else
		// 소형 BLDC 매핑
		switch(HallSum){
			case 6: Set_Phases( 0, 1, -1); break;
			case 4: Set_Phases(1, 0, -1); break;
			case 5: Set_Phases(1, -1, 0); break;
			case 1: Set_Phases( 0, -1, 1); break;
			case 3: Set_Phases( -1, 0, 1); break;
			case 2: Set_Phases( -1, 1, 0); break;
			default: Set_Phases(0,0,0); break;
		}
	#endif

}

void Update_Switching_Pattern(uint8_t HallSum)
{
	#if (MOTOR_DIR==MOTOR_DIR_CW)
		phases_fwd(HallSum);
	#else
		phases_rev(HallSum);
	#endif
}

unsigned int dir =1;


void Set_Phases(int32_t phaseA, int32_t phaseB, int32_t phaseC)
{
	DutyA=CNT_MAX-VoltageRef;
	DutyB=CNT_MAX-VoltageRef;
	DutyC=CNT_MAX-VoltageRef;

	if (VoltageRef==0 || StartFlag==0) {
	Disable_PWM();
	TIM1->CCR1 = TIM1->CCR2 = TIM1->CCR3 = 0;
	Mask_Channel(1); Mask_Channel(2); Mask_Channel(3);
	return;
	}

	// Phase A
	if (phaseA== 1) { TIM1->CCR1 = DutyA; Unmask_Channel(1); }
	else if (phaseA==-1) { TIM1->CCR1 = CNT_MAX; Unmask_Channel(1); }
	else { TIM1->CCR1 = 0; Mask_Channel(1); }

	// Phase B
	if (phaseB== 1) { TIM1->CCR2 = DutyB; Unmask_Channel(2); }
	else if (phaseB==-1) { TIM1->CCR2 = CNT_MAX; Unmask_Channel(2); }
	else { TIM1->CCR2 = 0; Mask_Channel(2); }

	// Phase C
	if (phaseC== 1) { TIM1->CCR3 = DutyC; Unmask_Channel(3); }
	else if (phaseC==-1) { TIM1->CCR3 = CNT_MAX; Unmask_Channel(3); }
	else { TIM1->CCR3 = 0; Mask_Channel(3); }

}


void Mask_Channel(uint8_t channel) {
    switch(channel) {
        case 1:
            TIM1->CCER &= ~(TIM_CCER_CC1E | TIM_CCER_CC1NE);
            break;
        case 2:
            TIM1->CCER &= ~(TIM_CCER_CC2E | TIM_CCER_CC2NE);
            break;
        case 3:
            TIM1->CCER &= ~(TIM_CCER_CC3E | TIM_CCER_CC3NE);
            break;
        default:
            // 유효하지 않은 채널 번호 처리 (필요 시)
            break;
    }
}

void Unmask_Channel(uint8_t channel) {
    switch(channel) {
        case 1:
            TIM1->CCER |= (TIM_CCER_CC1E | TIM_CCER_CC1NE);
            break;
        case 2:
            TIM1->CCER |= (TIM_CCER_CC2E | TIM_CCER_CC2NE);
            break;
        case 3:
            TIM1->CCER |= (TIM_CCER_CC3E | TIM_CCER_CC3NE);
            break;
        default:
            // 유효하지 않은 채널 번호 처리 (필요 시)
            break;
    }
}



