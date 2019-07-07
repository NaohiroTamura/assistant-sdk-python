#!/usr/bin/python
###########################################################################
#Filename      :lightsensor.py
#Description   :use a photoresistor to allow the LED to breathe
#Author        :alan
#Website       :www.osoyoo.com
#Update        :2017/06/29
############################################################################
# This code is released into the public domain

import time
import os
import RPi.GPIO as GPIO

# change these as desired - they're the pins connected from the
# SPI port on the ADC to the Cobbler
SPICLK = 11
SPIMISO = 9
SPIMOSI = 10
SPICS = 8
#set BCM_GPIO 18(GPIO1) as LED pin
#LEDPIN = 18

analogChannel = 0

#setup function for some setup---custom function
def setup():
    #global p
    GPIO.setwarnings(False)
    #set the gpio modes to BCM numbering
    GPIO.setmode(GPIO.BCM)
    # set up the SPI interface pins
    GPIO.setup(SPIMOSI, GPIO.OUT)
    GPIO.setup(SPIMISO, GPIO.IN)
    GPIO.setup(SPICLK, GPIO.OUT)
    GPIO.setup(SPICS, GPIO.OUT)
    #set all LedPin's mode to output,and initial level to HIGH(3.3V)
    #GPIO.setup(LEDPIN,GPIO.OUT,initial=GPIO.LOW)
    #set LEDPIN as PWM output,and frequency=100Hz
    #p = GPIO.PWM(LEDPIN,100)
    #set p begin with ualue 0
    #p.start(0)
    pass

    
#print message at the begining ---custom function
def print_message():
    print ('|**********************************|')
    print ('|   MCP3008 read lightsensor   |')
    print ('|   -----------------------------  |')
    print ('|    | ADC |           | Pi  |     |')
    print ('|    |-----|-----------|-----|     |')
    print ('|    | CS  | connect to| CE0 |     |')
    print ('|    | Din | connect to| MOSI|     |')
    print ('|    | Dout| connect to| MISO|     |')
    print ('|    | CLK | connect to| SCLK|     |')
    print ('|    | CH0 | connect to| 3.3V|     |')
    print ('|    | CH1 | connect to| GND |     |')
    print ('| ********************************** |')
    print (' LED connect to GPIO1')
    print ('|                            OSOYOO|')
    print ('|**********************************|\n')
    print ('Program is running...')
    print ('Please press Ctrl+C to end the program...')

# read SPI data from MCP3008 chip, 8 possible adc's (0 thru 7)
def readadc(adcnum, clockpin, mosipin, misopin, cspin):
        if ((adcnum > 7) or (adcnum < 0)):
                return -1
        GPIO.output(cspin, True)

        GPIO.output(clockpin, False)  # start clock low
        GPIO.output(cspin, False)     # bring CS low

        commandout = adcnum
        commandout |= 0x18  # start bit + single-ended bit
        commandout <<= 3    # we only need to send 5 bits here
        for i in range(5):
                if (commandout & 0x80):
                        GPIO.output(mosipin, True)
                else:
                        GPIO.output(mosipin, False)
                commandout <<= 1
                GPIO.output(clockpin, True)
                GPIO.output(clockpin, False)

        adcout = 0
        # read in one empty bit, one null bit and 10 ADC bits
        for i in range(12):
                GPIO.output(clockpin, True)
                GPIO.output(clockpin, False)
                adcout <<= 1
                if (GPIO.input(misopin)):
                        adcout |= 0x1

        GPIO.output(cspin, True)
        
        adcout >>= 1       # first bit is 'null' so drop it
        return adcout

def read_lightsensor_adc_ratio():
    adc = readadc(analogChannel, SPICLK, SPIMOSI, SPIMISO, SPICS)
    ratio = (1023-adc)*100/1023
    return ratio

#main function
def main():
    while True:
        #print info
        print_message()
        #adc = readadc(analogChannel, SPICLK, SPIMOSI, SPIMISO, SPICS)
        #print ('LightSensor ADC = %d' % (adc))
        #adc=(1023-adc)*100/1023
        #p.ChangeDutyCycle(int(adc))
        ratio = read_lightsensor_adc_ratio()
        print ('LightSensor ADC Ratio = %.2f' % (ratio))
        time.sleep(1)

#define a destroy function for clean up everything after the script finished
def destroy():
    #stop p
    #p.stop()
    #turn off led
    #GPIO.output(LEDPIN,GPIO.LOW)
    #release resource
    GPIO.cleanup()
    
#
# if run this script directly ,do:
if __name__ == '__main__':
    setup()
    try:
            main()
    #when 'Ctrl+C' is pressed,child program destroy() will be executed.
    except KeyboardInterrupt:
        destroy()

