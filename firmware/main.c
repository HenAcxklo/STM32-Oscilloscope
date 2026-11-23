/* main.c — STM32F103C8T6 ADC(PA0) + USART1(DMA)
 *
 * 帧：AA 55 + 256×int16（小端） + 5A A5 = 516 字节
 * 默认 2 Mbps（BRR 0x24）；链路不稳就改 0x4E（921600）或 0x271（115200）。
 *
 * 相比原来的“极速版”改了 4 处：
 *   1. 采样时间 1.5 周期太短，改成 7.5 周期，读数更稳；
 *   2. 校准后先读一次 DR，清掉残留 EOC，避免第一点读到旧值；
 *   3. DMA 初始化清标志、CNDTR 归零，Pack 前清 TC 标志，避免第一帧异常；
 *   4. 校准/EOC 等待都加了超时，不会卡死。
 */

#include "stm32f10x.h"
#include <stdint.h>

#define BUF_SIZE      256
#define FRAME_SIZE    (4 + BUF_SIZE * 2)

/* 72 MHz APB2：2 Mbps -> 0x24；921600 -> 0x4E；115200 -> 0x271 */
#define USART_BRR_VALUE   0x24

/* ADC 采样时间：0=1.5, 1=7.5, 2=13.5, 3=28.5 周期
 * 12 MHz 下 1.5 周期只有 125 ns，源阻抗稍高就采不准；7.5 周期更稳。 */
#define ADC_SMP_CODE      1

/* PA1 电位器：ADC1 通道 1，55.5 周期（电位器源阻抗高，采样时间要长） */
#define ADC_SMP_POT       5

/* 电位器 0~4095 映射到 PA0 采样间隔 0~200us
 * 200us 时约 5kSPS、256 点窗口 51ms；觉得太慢/太快改这个值即可。 */
#define POT_MAX_DELAY_US  200u

static int16_t raw_buf[BUF_SIZE];
static uint8_t tx_frame[FRAME_SIZE];
static volatile uint8_t dma_busy = 0;

/* ---------- 系统时钟：HSE 8MHz -> PLL x9 -> 72MHz ---------- */
void System_Clock_Init(void)
{
    RCC->CR |= RCC_CR_HSEON;
    while (!(RCC->CR & RCC_CR_HSERDY));

    FLASH->ACR &= ~FLASH_ACR_LATENCY;
    FLASH->ACR |= FLASH_ACR_LATENCY_2 | FLASH_ACR_PRFTBE;

    RCC->CFGR &= ~(RCC_CFGR_PLLSRC | RCC_CFGR_PLLMULL);
    RCC->CFGR |= RCC_CFGR_PLLSRC | RCC_CFGR_PLLMULL9;

    RCC->CR |= RCC_CR_PLLON;
    while (!(RCC->CR & RCC_CR_PLLRDY));

    RCC->CFGR &= ~(RCC_CFGR_HPRE | RCC_CFGR_PPRE1 | RCC_CFGR_PPRE2 | RCC_CFGR_ADCPRE);
    RCC->CFGR |= RCC_CFGR_PPRE1_DIV2;
    RCC->CFGR |= RCC_CFGR_ADCPRE_DIV6;          /* ADC 时钟 12 MHz */

    RCC->CFGR &= ~RCC_CFGR_SW;
    RCC->CFGR |= RCC_CFGR_SW_PLL;
    while ((RCC->CFGR & RCC_CFGR_SWS) != RCC_CFGR_SWS_PLL);
}

/* ---------- GPIO ---------- */
void MGPIO_Init(void)
{
    RCC->APB2ENR |= RCC_APB2ENR_IOPAEN;

    GPIOA->CRL &= ~(0xF << 0);                  /* PA0：模拟输入 */
    GPIOA->CRL &= ~(0xF << 4);                  /* PA1：模拟输入（电位器） */
    GPIOA->CRH &= ~(0xF << 4);
    GPIOA->CRH |=  (0xB << 4);                  /* PA9：复用推挽 50 MHz */
    GPIOA->CRH &= ~(0xF << 8);
    GPIOA->CRH |=  (0x4 << 8);                  /* PA10：浮空输入 */
}

/* ---------- 约 1 us 的粗延时（72 MHz 下 15 次循环，可按需微调） ---------- */
void Delay_us(uint32_t us)
{
    while (us--) {
        for (volatile uint32_t i = 0; i < 15; i++) __NOP();
    }
}

/* ---------- ADC1：单次转换，SWSTART 触发 ---------- */
void ADC1_Init(void)
{
    RCC->APB2ENR |= RCC_APB2ENR_ADC1EN;

    ADC1->CR1 = 0;
    ADC1->CR2 = 0;
    ADC1->SQR1 = 0;                             /* 规则组 1 个转换 */
    ADC1->SQR3 = 0;                             /* 先选通道 0：PA0 */
    ADC1->SMPR2 = (ADC_SMP_CODE << 0) | (ADC_SMP_POT << 3);

    ADC1->CR2 |= ADC_CR2_ADON;
    for (volatile uint32_t i = 0; i < 10000; i++);

    uint32_t timeout = 100000u;
    ADC1->CR2 |= ADC_CR2_RSTCAL;
    while ((ADC1->CR2 & ADC_CR2_RSTCAL) && --timeout);
    timeout = 100000u;
    ADC1->CR2 |= ADC_CR2_CAL;
    while ((ADC1->CR2 & ADC_CR2_CAL) && --timeout);

    /* 软件触发：EXTTRIG=1，EXTSEL=111（SWSTART） */
    ADC1->CR2 |= ADC_CR2_EXTTRIG | (7u << 17);

    (void)ADC1->DR;                             /* 清掉校准后残留的 EOC */
}

uint16_t ADC1_ReadChannel(uint8_t ch)
{
    ADC1->SQR3 = ch;                            /* 切换规则组通道 */
    ADC1->CR2 |= ADC_CR2_SWSTART;
    uint32_t timeout = 10000u;
    while (!(ADC1->SR & ADC_SR_EOC) && --timeout);
    return (uint16_t)ADC1->DR;
}

uint16_t ADC1_Read(void)
{
    ADC1->CR2 |= ADC_CR2_SWSTART;
    uint32_t timeout = 10000u;
    while (!(ADC1->SR & ADC_SR_EOC) && --timeout);
    return (uint16_t)ADC1->DR;
}

/* ---------- USART1 ---------- */
void USART1_Init(void)
{
    RCC->APB2ENR |= RCC_APB2ENR_USART1EN;

    USART1->BRR = USART_BRR_VALUE;
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;  /* 8N1 */
    USART1->CR2 = 0;
    USART1->CR3 = USART_CR3_DMAT;               /* 打开 TX DMA 请求 */
}

/* ---------- DMA1 通道 4：USART1_TX ---------- */
void DMA1_Channel4_Init(void)
{
    RCC->AHBENR |= RCC_AHBENR_DMA1EN;

    DMA1->IFCR = DMA_IFCR_CGIF4 | DMA_IFCR_CTCIF4 | DMA_IFCR_CHTIF4 | DMA_IFCR_CTEIF4;
    DMA1_Channel4->CCR = 0;
    DMA1_Channel4->CPAR = (uint32_t)&USART1->DR;
    DMA1_Channel4->CMAR = (uint32_t)tx_frame;
    DMA1_Channel4->CNDTR = 0;
    DMA1_Channel4->CCR = DMA_CCR1_DIR | DMA_CCR1_MINC | DMA_CCR1_PL_1 | DMA_CCR1_TCIE;

    NVIC_SetPriority(DMA1_Channel4_IRQn, 2);
    NVIC_EnableIRQ(DMA1_Channel4_IRQn);
}

void DMA1_Channel4_IRQHandler(void)
{
    if (DMA1->ISR & DMA_ISR_TCIF4) {
        DMA1->IFCR = DMA_IFCR_CTCIF4;
        DMA1_Channel4->CCR &= ~DMA_CCR1_EN;
        dma_busy = 0;
    }
}

/* ---------- 打包并发送 ---------- */
void PackAndSend(void)
{
    tx_frame[0] = 0xAA;
    tx_frame[1] = 0x55;
    for (int i = 0; i < BUF_SIZE; i++) {
        tx_frame[2 + i * 2]     = (uint8_t)(raw_buf[i] & 0xFF);
        tx_frame[2 + i * 2 + 1] = (uint8_t)((raw_buf[i] >> 8) & 0xFF);
    }
    tx_frame[2 + BUF_SIZE * 2]     = 0x5A;
    tx_frame[2 + BUF_SIZE * 2 + 1] = 0xA5;

    DMA1->IFCR = DMA_IFCR_CTCIF4;               /* 清残留完成标志 */
    DMA1_Channel4->CCR &= ~DMA_CCR1_EN;
    DMA1_Channel4->CMAR = (uint32_t)tx_frame;
    DMA1_Channel4->CNDTR = FRAME_SIZE;

    dma_busy = 1;
    DMA1_Channel4->CCR |= DMA_CCR1_EN;
}

/* ---------- 主函数 ---------- */
int main(void)
{
    System_Clock_Init();
    MGPIO_Init();
    USART1_Init();
    DMA1_Channel4_Init();
    ADC1_Init();

    while (1) {
        /* 读 4 次 PA1 电位器求平均，映射成 PA0 的采样间隔 0~200us */
        uint32_t pot_sum = 0;
        for (int k = 0; k < 4; k++) {
            pot_sum += ADC1_ReadChannel(1);
        }
        uint32_t delay_us = ((pot_sum / 4) * POT_MAX_DELAY_US) / 4095u;

        ADC1->SQR3 = 0;                         /* 切回 PA0 */

        for (int i = 0; i < BUF_SIZE; i++) {
            raw_buf[i] = (int16_t)ADC1_Read();
            Delay_us(delay_us);
        }

        PackAndSend();
        while (dma_busy);
    }
}
