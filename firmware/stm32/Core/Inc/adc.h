
#ifndef INC_ADC_H_
#define INC_ADC_H_
#include "stm32f767xx.h"

#define ADC_DMA_FRAME_LENGTH       6U
#define ADC_DMA_IAS_INDEX          0U
#define ADC_DMA_IBS_INDEX          1U
#define ADC_DMA_ICS_INDEX          2U
#define ADC_DMA_MOSFET_TEMP_INDEX  3U
#define ADC_DMA_THROTTLE_INDEX     4U
#define ADC_DMA_VDC_INDEX          5U

typedef struct
{
    uint16_t frame[2][ADC_DMA_FRAME_LENGTH];
    uint16_t padding[4];
} ADC_DMA_Buffer_t;

extern volatile ADC_DMA_Buffer_t AdcDmaBuffer;

void Initialize_ADC(void);
void Start_ADC_DMA(void);
void ADC_Offset_Cal(void);
#endif /* INC_ADC_H_ */
