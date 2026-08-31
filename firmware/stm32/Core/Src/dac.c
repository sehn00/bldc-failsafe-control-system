#include "dac.h"

void DAC_Init(void)
{
    /* 1. DAC 클럭 활성화 (매크로 사용) */
    RCC->APB1ENR |= RCC_APB1ENR_DACEN;

    /* 2. GPIOA 클럭 활성화 (PA4, PA5 사용) */
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOAEN;

    /* 3. PA4, PA5를 아날로그 모드로 설정
       - Analog 모드: MODER 값 = 0b11 (매크로 사용 없이 직접 0x3 << (pin*2) 방식)
       - 풀업/풀다운 없음 */
    GPIOA->MODER &= ~((0x3UL << (4 * 2)) | (0x3UL << (5 * 2)));
    GPIOA->MODER |=  ((0x3UL << (4 * 2)) | (0x3UL << (5 * 2)));
    GPIOA->PUPDR &= ~((0x3UL << (4 * 2)) | (0x3UL << (5 * 2)));

    /* 4. DAC 채널 설정
       - 채널1: PA4, 채널2: PA5
       - 트리거 사용하지 않고(software mode), 내부 버퍼를 사용
       - DAC->CR의 매크로(예: DAC_CR_EN1, DAC_CR_TEN1, DAC_CR_BOFF1 등)를 활용 */
    DAC->CR &= ~((DAC_CR_BOFF1 | DAC_CR_TEN1) | (DAC_CR_BOFF2 | DAC_CR_TEN2));
    DAC->CR |= (DAC_CR_EN1 | DAC_CR_EN2);
}

void DAC_SetValue(uint16_t value) {
    DAC->DHR12R1 = value & 0x0FFF;
}

void DAC_SetValue_Ch2(uint16_t val)
{
    // 12비트 정렬(오른쪽 정렬 DHR12R2 레지스터)
    DAC->DHR12R2 = (val & 0x0FFF);
}
