
#ifndef INC_TIMER_H_
#define INC_TIMER_H_

#include "stm32f767xx.h"
#define CNT_MAX			5400

void Initialize_PWM(void);
void Initialize_TIM2(void);
void Enable_PWM(void);
void Disable_PWM(void);

#endif /* INC_TIMER_H_ */
