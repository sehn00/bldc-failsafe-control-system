
#ifndef INC_HALL_H_
#define INC_HALL_H_
#include "stm32f767xx.h"
#include "timer.h"
void Initialize_Hall_Sensors(void);
void Update_Switching_Pattern(uint8_t hall);
void Set_Phases(int32_t phaseA, int32_t phaseB, int32_t phaseC);
void Unmask_Channel(uint8_t channel);
void Mask_Channel(uint8_t channel);
void phases_fwd(uint8_t HallSum);
void phases_rev(uint8_t HallSum);
extern uint32_t VoltageRef;
extern unsigned int dir;
#endif /* INC_HALL_H_ */
