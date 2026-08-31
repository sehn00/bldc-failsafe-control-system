#include "uart.h"
#include "clock.h"

void AT09_Init(void) // BT
{
	// 1) USART3 클럭 활성화

	RCC->APB1ENR |= RCC_APB1ENR_USART3EN;

	// 2) GPIOD 클럭 활성화 (PD8, PD9)
	RCC->AHB1ENR |= RCC_AHB1ENR_GPIODEN;

	// 3) PD8(TX), PD9(RX) → Alternate Function AF7
	GPIOD->MODER &= ~((3UL << (8 * 2)) | (3UL << (9 * 2)));
	GPIOD->MODER |=  ((2UL << (8 * 2)) | (2UL << (9 * 2)));

	GPIOD->AFR[1] &= ~((0xFUL << ((8 - 8) * 4)) | (0xFUL << ((9 - 8) * 4)));
	GPIOD->AFR[1] |=  ((7UL   << ((8 - 8) * 4)) | (7UL   << ((9 - 8) * 4)));

	// 4) USART3 레지스터 초기화
	USART3->CR1 = 0;
	USART3->CR2 = 0;
	USART3->CR3 = 0;

	// 5) Baud Rate 9600bps @ APB1=54MHz → BRR = 5625
	USART3->BRR = 5625;


	// 6) TE=1, RE=1, UE=1
	USART3->CR1 |= (USART_CR1_TE | USART_CR1_RE);
	USART3->CR1 |= USART_CR1_UE;
}

void USART3_SendChar(char c)
{
    // TXE=1(전송 데이터 레지스터 비어있음) 대기
    while (!(USART3->ISR & USART_ISR_TXE));
    USART3->TDR = (uint8_t)c;
}

// 문자열 송신
void USART3_SendString(const char *str)
{
    while (*str)
    {
        USART3_SendChar(*str++);
    }
}
// 문자 1개 수신 (블로킹)
uint32_t USART_Timeout = 0;
char USART3_ReceiveChar(void)
{
    // RXNE=1이 될 때까지 대기
    while (!(USART3->ISR & USART_ISR_RXNE))
    {
        if(USART_Timeout>5000)
        {
        	USART_Timeout = 0;

        	break;
        }
        else
        {
        	USART_Timeout++;
        }
    }
    return (char)(USART3->RDR & 0xFF);


}

// 문자열 수신 (개행 문자로 종료) - 매우 간단 버전

void USART3_ReceiveString(char *buffer, uint32_t maxLen)
{
    if (maxLen == 0) return;

    uint32_t i = 0;
    while (1)
    {
        char c = USART3_ReceiveChar();  // 블로킹 수신

        // 종료 문자(개행) 검사
        if (c == '\n' || c == '\r')
        {
            buffer[i] = '\0';  // 문자열 종료
            break;
        }

        // 버퍼 저장
        buffer[i++] = c;
        // 버퍼 오버플로 방지
        if (i >= (maxLen - 1))
        {
            buffer[i] = '\0';
            break;
        }

    }
}
void UART3_SendFloat_Simple(float value, int decimals)
{
    // 1) 부호 처리
    if(value < 0.0f)
    {
    	USART3_SendChar('-');
        value = -value;  // 양수화
    }

    // 2) 정수부 추출
    int32_t iPart = (int32_t)value;       // 정수부
    float   frac  = value - (float)iPart; // 소수부(0 <= frac < 1)

    // 3) 정수부를 ASCII 변환하여 전송 (간단 구현)
    //    ex) iPart=123 -> "123"
    {
        char tmpBuf[12];
        int idx = 0;

        if(iPart == 0)
        {
            tmpBuf[idx++] = '0';
        }
        else
        {
            // iPart가 0이 아니라면, 역순으로 숫자를 추출
            while(iPart > 0 && idx < (int)(sizeof(tmpBuf)-1))
            {
                tmpBuf[idx++] = (char)('0' + (iPart % 10));
                iPart /= 10;
            }
        }

        // tmpBuf에 저장된 숫자를 다시 뒤집어서 전송
        while(idx > 0)
        {
        	USART3_SendChar(tmpBuf[--idx]);
        }
    }

    // 4) 소수 자릿수 처리 (decimals > 0인 경우)
    if(decimals > 0)
    {
    	USART3_SendChar('.');  // 소수점 출력

        for(int i=0; i<decimals; i++)
        {
            frac *= 10.0f;
            int digit = (int)frac;       // 0 ~ 9
            USART3_SendChar((char)('0' + digit));
            frac -= digit;               // 소수점 이하 갱신
        }
    }
}


//------------------------------
// UART2 초기화 (RPi 통신용 115200bps)
//------------------------------
void UART2_Init(void)
{
    // 1) GPIOD, USART2 클럭 활성화
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIODEN;
    RCC->APB1ENR |= RCC_APB1ENR_USART2EN;

    // 2) PD5=TX, PD6=RX → AF7 설정
    GPIOD->MODER &= ~((3U << (5*2)) | (3U << (6*2)));
    GPIOD->MODER |=  ((2U << (5*2)) | (2U << (6*2)));
    GPIOD->AFR[0]  &= ~((0xFU << (5*4)) | (0xFU << (6*4)));
    GPIOD->AFR[0]  |=  ((7U   << (5*4)) | (7U   << (6*4)));

    // 3) USART2 레지스터 초기화
    USART2->CR1 = 0;
    USART2->CR2 = 0;
    USART2->CR3 = 0;

    // 4) 보레이트 115200bps 설정 @PCLK1=54MHz
    //    54_000_000 / 115_200 = 468.75 -> BRR = 469
    USART2->BRR = 469;

    // 5) 송수신 + RXNE 인터럽트 + USART2 활성화
    USART2->CR1 |= (USART_CR1_TE | USART_CR1_RE | USART_CR1_RXNEIE);
    USART2->CR1 |= USART_CR1_UE;

    // 6) USART2 RX 인터럽트 활성화
    NVIC_EnableIRQ(USART2_IRQn);
}

//------------------------------
// 한글로 속도 전송 예시
//------------------------------
void ReportSpeed(float rpm)
{
    UART2_SendString("속도: ");              // 한글 “속도: ”
    UART2_SendFloat_Simple(rpm, 0);          // 소수점 0자리
    UART2_SendString(" RPM\r\n");           // 단위 표시
}

void UART2_SendChar(char c)
{
    // TXE (송신 데이터 레지스터 비어있음) 플래그 대기
    while (!(USART2->ISR & USART_ISR_TXE));
    USART2->TDR = (uint8_t)c;
}
char UART2_ReceiveChar(void)
{
    // RXNE (수신 데이터 레지스터에 데이터 있음) 플래그 대기
    while (!(USART2->ISR & USART_ISR_RXNE));
    return (char)(USART2->RDR & 0xFF);
}
void UART2_SendString(const char *str)
{
    while (*str)
    {
    	UART2_SendChar(*str++);
    }
}

void UART2_SendFloat_Simple(float value, int decimals)
{
    // 1) 부호 처리
    if(value < 0.0f)
    {
    	UART2_SendChar('-');
        value = -value;  // 양수화
    }

    // 2) 정수부 추출
    int32_t iPart = (int32_t)value;       // 정수부
    float   frac  = value - (float)iPart; // 소수부(0 <= frac < 1)

    // 3) 정수부를 ASCII 변환하여 전송 (간단 구현)
    //    ex) iPart=123 -> "123"
    {
        char tmpBuf[12];
        int idx = 0;

        if(iPart == 0)
        {
            tmpBuf[idx++] = '0';
        }
        else
        {
            // iPart가 0이 아니라면, 역순으로 숫자를 추출
            while(iPart > 0 && idx < (int)(sizeof(tmpBuf)-1))
            {
                tmpBuf[idx++] = (char)('0' + (iPart % 10));
                iPart /= 10;
            }
        }

        // tmpBuf에 저장된 숫자를 다시 뒤집어서 전송
        while(idx > 0)
        {
        	UART2_SendChar(tmpBuf[--idx]);
        }
    }

    // 4) 소수 자릿수 처리 (decimals > 0인 경우)
    if(decimals > 0)
    {
    	UART2_SendChar('.');  // 소수점 출력

        for(int i=0; i<decimals; i++)
        {
            frac *= 10.0f;
            int digit = (int)frac;       // 0 ~ 9
            UART2_SendChar((char)('0' + digit));
            frac -= digit;               // 소수점 이하 갱신
        }
    }
}
