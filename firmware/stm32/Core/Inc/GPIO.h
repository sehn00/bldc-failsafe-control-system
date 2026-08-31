
#ifndef INC_GPIO_H_
#define INC_GPIO_H_
void BT_EN_PIN(void);
void BT_Eable(void);
void BT_Disable(void);
void FLT_LED_Init(void);
void FLT_LED_OnOff(uint8_t on);
void Configure_PE15_As_Output(void);
void ADC_PE15_On(void);
void ADC_PE15_Off(void);
#endif /* INC_GPIO_H_ */
