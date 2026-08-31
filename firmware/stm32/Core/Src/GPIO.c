#include "adc.h"

// PC6 핀 초기화 함수
void FLT_LED_Init(void)
{
    // 1) GPIOC 클럭 활성화
    RCC->AHB1ENR |= 0x00000004;    // GPIOCEN

    // 2) PC6을 General-purpose output 모드로 설정 (MODER6[1:0] = 01)
    GPIOC->MODER  &= ~0x00003000;  // clear bits 13:12
    GPIOC->MODER  |=  0x00001000;  // set bit 12

    // 3) PC6 중간 속도 설정 (OSPEEDR6[1:0] = 01)
    GPIOC->OSPEEDR &= ~0x00003000;  // clear bits 13:12
    GPIOC->OSPEEDR |=  0x00001000;  // set bit 12
}

// PC6 On/Off 제어 함수
void FLT_LED_OnOff(uint8_t on)
{
    if(on)
    {
        // LED On
        GPIOC->ODR &= ~0x00000040; // clear bit 6
    }
    else
    {
        // LED Off
        GPIOC->ODR |=  0x00000040; // set bit 6
    }
}
