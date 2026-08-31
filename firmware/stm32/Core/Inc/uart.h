#ifndef __UART_H
#define __UART_H

#include "stm32f767xx.h"
void AT09_Init(void);
void USART3_SendChar(char c);
void USART3_SendString(const char *str);
char USART3_ReceiveChar(void);
void USART3_ReceiveString(char *buffer, uint32_t maxLen);
void UART3_SendFloat_Simple(float value, int decimals);
void UART2_Init(void);
void UART2_SendChar(char c);
char UART2_ReceiveChar(void);
void UART2_SendString(const char *str);
void UART2_SendFloat_Simple(float value, int decimals);
#endif  // __UART_H
