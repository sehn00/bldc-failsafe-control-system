#include "clock.h"

/* main.c 의 SysTick_Handler() 가 1ms 마다 증가시키는 글로벌 카운터.
 * Delay_ms 구현에서 이 값을 스냅샷 비교하여 경과 시간을 측정한다.
 * 사용 전에 반드시 SysTick_Init() 이 호출되어 있어야 함 — 아니면 무한 대기. */
extern volatile uint32_t msTicks;

void Delay_ms(uint32_t time_ms)
{
	uint32_t start = msTicks;
	while ((msTicks - start) < time_ms) { /* busy-wait */ }
}

void Initialize_MCU(void)			/* initialize STM32F767VGT6 MCU */
{
// 데이터 캐시 사용하여 코드 실행 속도 향상
  SCB_EnableICache();				// enable L1 instruction cache
  SCB_EnableDCache();				// enable L1 data cache

// ART 가속기, 프리페치 버퍼, 웨이트 사이클 설정
  FLASH->ACR = 0x00000307;			// 7 waits, enable ART accelerator and prefetch

// HSE,PLL 설정 시스템 클록 : 216MHz
  RCC->CR |= 0x00010001;			// HSE on, HSI on
  while((RCC->CR & 0x00000002) == 0);		// wait until HSIRDY = 1
  RCC->CFGR = 0x00000000;			// SYSCLK = HSI
  while((RCC->CFGR & 0x0000000C) != 0);		// wait until SYSCLK = HSI

  RCC->CR = 0x00010001;				// PLL off, HSE on, HSI on
  RCC->PLLCFGR = 0x09403608;			// SYSCLK = HSE*PLLN/PLLM/PLLP = 16MHz*216/8/2 = 216MHz
						// PLL48CK = HSE*PLLN/PLLM/PLLQ = 16MHz*216/8/9 = 48MHz
  RCC->CR = 0x01010001;				// PLL on, HSE on, HSI on
  while((RCC->CR & 0x02000000) == 0);		// wait until PLLRDY = 1

// 216MHz 사용을 위해 오버드라이브 설정
  RCC->APB1ENR |= 0x10000000;			// 전원모듈 클록(PWREN = 1)
  PWR->CR1 |= 0x00010000;			// over-drive enable(ODEN = 1)
  while((PWR->CSR1 & 0x00010000) == 0);		// ODRDY = 1 ?
  PWR->CR1 |= 0x00020000;			// over-drive switching enable(ODSWEN = 1)
  while((PWR->CSR1 & 0x00020000) == 0);		// ODSRDY = 1 ?

// 최종 클록 분주비를 설정 SYSCLK = PLL, AHB = 216MHz,APB1CLK = APB2CLK = 54MHz
  RCC->CFGR = 0x3040B402;			// SYSCLK = PLL, AHB = 216MHz, APB1 = APB2 = 54MHz
  RCC->DCKCFGR1 = 0x01000000;			// TIMxCLK = 216MHz
  while((RCC->CFGR & 0x0000000C) != 0x00000008);// wait until SYSCLK = PLL
  RCC->CR |= 0x00080000;			// CSS on

// (6) I/O 보상 설정
  RCC->APB2ENR |= 0x00004000;			// 주변장치 클럭(SYSCFG = 1)
  SYSCFG->CMPCR = 0x00000001;			// enable compensation cell

  /* 1. DAC 클럭 활성화 (매크로 사용) */
  RCC->APB1ENR |= RCC_APB1ENR_DACEN;
  /* 2. GPIOA 클럭 활성화 (PA4, PA5 사용) */
  RCC->AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
  // 1. USART2 및 관련 GPIO 클럭 활성화
  //    USART2는 APB1 버스에 연결되어 있음
  RCC->APB1ENR |= RCC_APB1ENR_USART2EN;   // USART2 클럭 활성화

  // 2. GPIOD 클럭 활성화 (PD5, PD6 사용)
  RCC->AHB1ENR |= RCC_AHB1ENR_GPIODEN;    // GPIOD 클럭 활성화
  // TIM2 클럭 활성화, 54Mhz
  RCC->APB1ENR |= RCC_APB1ENR_TIM2EN;
  //ADC1,ADC2,ADC3,TIM1
  RCC->APB2ENR |= 0x00000701;			// 주변장치 클럭(ADC3 = ADC2 = ADC1 = TIM1 = 1)
  RCC->AHB1ENR |= 0x0000001F;
}


