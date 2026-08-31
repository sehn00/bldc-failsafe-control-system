
#define DEADTIME_1us 	((uint32_t)180)
#include "timer.h"
#include "hall.h"

void Initialize_PWM(void)			/* initialize TIM1 for PWM */
{
	GPIOE->MODER  &= 0x0000FFFF;			// use TIM1_CH3/3N/2/2N/1/1N
	GPIOE->MODER  |= 0x9AAA0000;
	GPIOE->AFR[1] |= 0x10111111;			// PE13~PE8 = TIM1_CH3/3N/2/2N/1/1N
	GPIOE->OSPEEDR |= 0x0FFF0000;			// PWM signal = 180MHz
	RCC->APB2ENR |= 0x00000001;			// enable TIM1 clock
	//216MHz

	TIM1->PSC = 0x0000;				// 216MHz/(0+1) = 216MHz
	TIM1->ARR = CNT_MAX;				// 216MHz/CNT_MAX/2 = 20kHz
	TIM1->CCMR1 = 0x00007878;			// TIM1_CH2/2N/1/1N = PWM mode 2
	TIM1->CCMR2 = 0x00000078;			// TIM1_CH3/3N = PWM mode 2
	TIM1->CCER = 0x00000555;			// TIM1_CH3/3N/2/2N/1/1N = active high
	TIM1->BDTR = 0x00000C00;			// OSSR = OSSI = 1
	TIM1->BDTR |= TIM_BDTR_MOE | DEADTIME_1us;
	TIM1->DIER = 0x0001;				// enable update interrupt
	TIM1->CR2 = 0x00000020;			// update event TRGO for ADC trigger
	TIM1->CR1 = 0x0065;				// center-aligned mode 3, URS = CEN = 1
}

void Initialize_TIM2(void)
{
    RCC->APB1ENR |= RCC_APB1ENR_TIM2EN;   // TIM2 클럭 인가

    /*  ── 수정 1 : PSC = 3  ──────────────────────────────
       TIMPRE(=1) 이라 TIM2 입력은 216 MHz → 216 MHz/(3+1)=54 MHz  */
    TIM2->PSC = 3;                        // 54 MHz tick

    /* 54 MHz × 1.000 s = 54 000 000-1 */
    TIM2->ARR = 53999999;                 // 1 s 오버플로우
    TIM2->CNT = 0;

    TIM2->CR1 |= TIM_CR1_CEN;             // 타이머 스타트
}

void Enable_PWM(void)				/* enable PWM output */
{
	TIM1->BDTR |= 0x00008000;			// MOE = 1			// MOE = 1
}

void Disable_PWM(void)				/* disable PWM output */
{
	TIM1->BDTR &= 0xFFFF7FFF;			// MOE = 0
	Mask_Channel(1);
	Mask_Channel(2);
	Mask_Channel(3);
}
