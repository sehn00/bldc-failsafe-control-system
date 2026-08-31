#ifndef __DAC_H
#define __DAC_H

#include "stm32f767xx.h"

void DAC_Init(void);
void DAC_SetValue(uint16_t value);
void DAC_SetValue_Ch2(uint16_t val);
#endif  // __DAC_H
