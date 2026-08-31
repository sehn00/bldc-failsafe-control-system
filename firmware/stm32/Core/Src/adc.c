
#include "adc.h"

#define ADC_TIM1_TRGO  (ADC_CR2_EXTSEL_3 | ADC_CR2_EXTSEL_0)
#define DMA2_STREAM4_ALL_FLAGS \
    (DMA_HIFCR_CTCIF4 | DMA_HIFCR_CHTIF4 | DMA_HIFCR_CTEIF4 | \
     DMA_HIFCR_CDMEIF4 | DMA_HIFCR_CFEIF4)

volatile ADC_DMA_Buffer_t AdcDmaBuffer
    __attribute__((section(".dma_buffer"), aligned(32)));

_Static_assert(sizeof(ADC_DMA_Buffer_t) == 32U,
               "ADC DMA buffer must occupy one cache line");

void Initialize_ADC(void)
{
    // GPIO 초기화
    GPIOA->MODER = 0xA955FFFF;  // PA0-PA7 아날로그 모드
    GPIOA->AFR[1] = 0x00000000;
    GPIOA->AFR[0] = 0x00000000;
    GPIOA->ODR = 0x00000000;
    GPIOA->OSPEEDR = 0xFC000000;

    // ADC 클럭 활성화
    RCC->APB2ENR |= 0x00000700;  // ADC1, ADC2, ADC3 클럭 활성화

    // ADC 공통 설정 - 독립 모드
    ADC->CCR = 0x00000000;  // 독립 모드, ADCCLK = PCLK2/2

    // ADC 샘플링 시간 설정 (모든 채널 15 사이클)
    ADC1->SMPR2 = 0x00249249;
    ADC2->SMPR2 = 0x00249249;
    ADC3->SMPR2 = 0x00249249;

    // ADC1 설정 - PA0(IN0), PA6(IN6)
    ADC1->CR1 = 0x00000000;
    ADC1->CR2 = 0x00000000;
    ADC1->SQR1 = 0x00000000;  // 1개 변환 (L=0)
    ADC1->SQR3 = 0x00000000;  // SQ1=0 (채널 0)

    // ADC2 설정 - PA1(IN1), PA7(IN7)
    ADC2->CR1 = 0x00000000;
    ADC2->CR2 = 0x00000000;
    ADC2->SQR1 = 0x00000000;  // 1개 변환 (L=0)
    ADC2->SQR3 = 0x00000001;  // SQ1=1 (채널 1)

    // ADC3 설정 - PA2(IN2), PA3(IN3)
    ADC3->CR1 = 0x00000000;
    ADC3->CR2 = 0x00000000;
    ADC3->SQR1 = 0x00000000;  // 1개 변환 (L=0)
    ADC3->SQR3 = 0x00000002;  // SQ1=2 (채널 2)

    // ADC 활성화
    ADC1->CR2 |= 0x00000001;  // ADON = 1
    ADC2->CR2 |= 0x00000001;
    ADC3->CR2 |= 0x00000001;
}

void Start_ADC_DMA(void)
{
    RCC->AHB1ENR |= RCC_AHB1ENR_DMA2EN;

    DMA2_Stream4->CR &= ~DMA_SxCR_EN;
    while (DMA2_Stream4->CR & DMA_SxCR_EN) { }

    for (uint32_t frame = 0U; frame < 2U; frame++)
    {
        for (uint32_t sample = 0U; sample < ADC_DMA_FRAME_LENGTH; sample++)
        {
            AdcDmaBuffer.frame[frame][sample] = 0U;
        }
    }
    SCB_CleanInvalidateDCache_by_Addr((uint32_t *)&AdcDmaBuffer,
                                     sizeof(AdcDmaBuffer));

    DMA2->HIFCR = DMA2_STREAM4_ALL_FLAGS;
    DMA2_Stream4->PAR = (uint32_t)&ADC->CDR;
    DMA2_Stream4->M0AR = (uint32_t)&AdcDmaBuffer.frame[0][0];
    DMA2_Stream4->M1AR = (uint32_t)&AdcDmaBuffer.frame[1][0];
    DMA2_Stream4->NDTR = ADC_DMA_FRAME_LENGTH;
    DMA2_Stream4->FCR = 0U;
    DMA2_Stream4->CR = DMA_SxCR_PL | DMA_SxCR_MSIZE_0 |
                       DMA_SxCR_PSIZE_0 | DMA_SxCR_MINC |
                       DMA_SxCR_CIRC | DMA_SxCR_DBM;

    /* Rank 1: phase currents, rank 2: temperature/throttle/Vdc. */
    ADC1->CR1 = ADC_CR1_SCAN;
    ADC2->CR1 = ADC_CR1_SCAN;
    ADC3->CR1 = ADC_CR1_SCAN;

    ADC1->SQR1 = ADC_SQR1_L_0;
    ADC2->SQR1 = ADC_SQR1_L_0;
    ADC3->SQR1 = ADC_SQR1_L_0;
    ADC1->SQR2 = 0U;
    ADC2->SQR2 = 0U;
    ADC3->SQR2 = 0U;
    ADC1->SQR3 = (0U << 0) | (6U << 5);
    ADC2->SQR3 = (1U << 0) | (7U << 5);
    ADC3->SQR3 = (2U << 0) | (3U << 5);

    ADC1->CR2 = ADC_CR2_ADON;
    ADC2->CR2 = ADC_CR2_ADON;
    ADC3->CR2 = ADC_CR2_ADON;

    ADC->CCR = ADC_CCR_MULTI_4 | ADC_CCR_MULTI_2 | ADC_CCR_MULTI_1 |
               ADC_CCR_DMA_0 | ADC_CCR_DDS;

    DMA2_Stream4->CR |= DMA_SxCR_EN;
    ADC1->CR2 |= ADC_TIM1_TRGO | ADC_CR2_EXTEN_0;

    /* Do not enable the control ISR until one complete DMA frame exists. */
    while ((DMA2->HISR & DMA_HISR_TCIF4) == 0U) { }
    DMA2->HIFCR = DMA2_STREAM4_ALL_FLAGS;
}





