#!/usr/bin/python3

#the main file, consisting of a loop that reads out the command queue
#and status queue, which are filled by several threads also started
#through this file. Also contains the lamp setter (a big if statement)
#and sensor functions

import time
import sys
import threading
import datetime
import os
import traceback

from subprocess import check_output
from queue import Queue

import lamp_control
import tsl2561
import temp_sensor
import http_commands
import telegram_bot

'''Reading configuration'''
from parsed_config import config

USER_MAC = config['bluetooth']['USER_MAC']
USER_NAME = config['bluetooth']['USER_NAME']

BLUETOOTH_RATE = int(config['rates']['BLUETOOTH'])
LIGHT_SENSOR_RATE = int(config['rates']['LIGHT_SENSOR'])
TEMP_SENSOR_RATE = int(config['rates']['TEMP_SENSOR'])
TIME_RATE = int(config['rates']['TIME'])

CURTAIN_THRESHOLD = int(config['thresholds']['CURTAIN'])
CURTAIN_ERROR = int(config['thresholds']['LIGHT_ERROR'])
PRESENT_THRESHOLD = int(config['thresholds']['PRESENT'])

THIS_FILE = os.path.dirname(__file__)

'''Helper functions'''

#writes <formatted date>\t<message>\n to log file <filename>
#parameters: date, filename, message
def write_log(message, filename="server_log", date_format=True):
    date = datetime.datetime.now()
    with open(os.path.join(THIS_FILE, "logs", filename), 'a') as f:
        if date_format is False:
            f.write("{}\t{}\n".format(time.time(), message))
        else:
            date_string = date.strftime("%Y-%m-%d %H:%M:%S")
            f.write("{}\t{}\n".format(date_string, message))
        f.close()

#simple lamp setting function to set lamps to daytime according to user presence
#only run when something's changed
def lamp_setter(override, priority_change, present, curtain, night_mode):
    if not override or priority_change: #we are on auto or the change is important
        if present and curtain and not night_mode: #lamps should be on
            new_colour, new_bright = lamp_control.set_to_cur_time(init=True)
            new_off = False
            write_log("lamps set to on, with automatic configuration")
        else: #lamps should be off
            lamp_control.set_off()
            new_colour, new_bright = None, None
            new_off = True
            write_log("lamps set to off")
    else: #we are on override and the change has no priority over it
        new_colour, new_bright = None, None
        new_off = None
        
    return new_off, new_colour, new_bright

#starts a thread running a function with some arguments, default not as daemon
#parameters: function, arguments (tuple), as_daemon (bool)

def thread_exception_handling(function, args):
    try:
        function(*args)
    except:
        write_log(traceback.format_exception_only(sys.exc_info()[0], sys.exc_info()[1])[0][:-1])

def start_thread(function, args, as_daemon=False):
    new_thread = threading.Thread(target=thread_exception_handling, args=(function,args))
    new_thread.daemon = as_daemon
    new_thread.start()

'''Main function'''

#reads commands from the queue and controls everything
def main_function(commandqueue, statusqueue, present_event, day_event):
    #init
    present = None
    not_present_count = 0
    
    curtain = None
    
    night_mode = False
    night_light = False
    
    light_level = -1
    
    override = False
    override_detected = 0 #counts number of times we have found the lamps not on auto
    
    #nothing has changed yet
    change = False
    priority_change = False
    
    lamps_off = None
    lamps_colour = None
    lamps_bright = None
    
    while True:
        command = commandqueue.get(block=True)
        
        #bluetooth checking: sets present
        if "bluetooth:"+USER_NAME in command:
            if "in" in command:
                present = True
                not_present_count = 0
                present_event.set()
            elif "out" in command:
                not_present_count += 1
                if not_present_count > PRESENT_THRESHOLD: #we are sure the user is gone
                    present_event.clear()
                    present = False
            priority_change = True
                
        
        #time checking: sets new hour and minute
        elif "time" in command:
            hour = int(command[5:7])
            minute = int(command[8:10])
            change = True
        
        #sensor checking: sets temp and light_level
        elif "sensors:temp" in command:
            temp = float(command[13:])
            year_month = datetime.datetime.now().strftime("%Y-%m")
            write_log(str(temp), filename="temp_log", date_format=None)
            write_log(str(temp), filename="temp_log"+"_"+year_month, date_format=None)
        
        elif "sensors:light" in command:
            light_level = int(command[14:])
            write_log(light_level, "light_log")
            if light_level > CURTAIN_THRESHOLD + CURTAIN_ERROR:
                curtain = False
            elif light_level < CURTAIN_THRESHOLD - CURTAIN_ERROR:
                curtain = True
            change = True
        
        elif "http:request_status" in command:
            status = {'light_level':light_level, 'curtain':curtain,
                      'temp':temp, 'night_mode':night_mode,
                      'present':present, 'lamps_colour':lamps_colour,
                      'lamps_bright':None, 'lamps_off':lamps_off
                     }
            if lamps_bright:
                status['lamps_bright'] = round((lamps_bright/255)*100)
            statusqueue.put(status)
        
        elif "http:command" in command:
            http_command = command[13:]
            if http_command == "night_on":
                night_mode = True
                priority_change = True
                day_event.clear()
            elif http_command == "night_off":
                night_mode = False
                priority_change = True
                day_event.set()
            elif http_command == "night_light_on":
                lamps_off = False
                lamps_colour, lamps_bright = lamp_control.night_light_on()
            elif http_command == "night_light_off":
                lamps_off = True
                lamps_colour, lamps_bright = None, None
                lamp_control.set_off()
        
        #unimplemented or faulty commands
        else:
            write_log("unknown command: {}".format(command))
        
        if lamp_control.is_override(): #override detected
            if not override: #start of override
                override_starttime = datetime.datetime.now()
                override = True
                write_log("override mode enabled")
        else: #auto detected
            if override: #a change
                override = False
                write_log("override mode disabled")
        
        #override timeout
        if override:
            if datetime.datetime.now() - override_starttime >= datetime.timedelta(hours=2):
                override = False
                write_log("override timed out")
        
        #setting the lights if something has changed
        if change or priority_change:
            new_off, new_colour, new_bright = lamp_setter(override, priority_change, present, curtain, night_mode)
            change = False
            priority_change = False
        if new_off is not None:
            lamps_off = new_off
        if new_colour:
            lamps_colour = new_colour
        if new_bright:
            lamps_bright = new_bright
        
        commandqueue.task_done()
    write_log("server stopped")
    


'''Thread functions'''

#send the time to the main thread every certain number of minutes
#commands: time:<hour>:<minute>
#parameters: commandqueue
#config: rate
def time_function(commandqueue):
    while True:
        #wait TIME_RATE minutes between each check
        minute = datetime.datetime.now().minute
        time.sleep((TIME_RATE - (minute % TIME_RATE)) * 60)
        
        #check if we need to send a command, if so send it
        hour = datetime.datetime.now().hour
        minute = datetime.datetime.now().minute
        if datetime.time(hour, minute) in lamp_control.light_by_time[0]:
            cur_time = datetime.datetime.now().strftime("%H:%M")
            command = "time:{}".format(cur_time)
            commandqueue.put(command)

#check the bluetooth presence of the user at a certain rate
#commands: bluetooth:<user_name>:[in, out]
#parameters: commandqueue
#config: rate, user_mac, user_name
def bluetooth_function(commandqueue, day_event):
    while True:
        day_event.wait()
        
        start = datetime.datetime.now()
        name = check_output(["hcitool", "name", USER_MAC]).decode("utf-8")[:-1]
        
        if name == USER_NAME:
            commandqueue.put("bluetooth:{}:in".format(USER_NAME))
        else:
            commandqueue.put("bluetooth:{}:out".format(USER_NAME))
        
        end = datetime.datetime.now()
        dt = (end - start).total_seconds()
        if BLUETOOTH_RATE > dt:
            time.sleep(BLUETOOTH_RATE-dt)
        

def temp_sensor_function(commandqueue):
    while True:
        start = datetime.datetime.now()

        temp = round(temp_sensor.read_temp(), 1)
        commandqueue.put("sensors:temp:{}".format(temp))
        
        end = datetime.datetime.now()
        dt = (end - start).total_seconds()
        if TEMP_SENSOR_RATE > dt:
            time.sleep(TEMP_SENSOR_RATE-dt)

def light_sensor_function(commandqueue, present_event, day_event):
    tsl = tsl2561.TSL2561()
    while True:
        present_event.wait()
        day_event.wait()
        
        start = datetime.datetime.now()
        
        light = int(tsl.lux())
        commandqueue.put("sensors:light:{}".format(light))
        
        end = datetime.datetime.now()
        dt = (end - start).total_seconds()
        if LIGHT_SENSOR_RATE > dt:
            time.sleep(LIGHT_SENSOR_RATE-dt)

if __name__ == '__main__':
    write_log("starting server")
    commandqueue = Queue()
    statusqueue = Queue()
    telegramqueue = Queue()
    
    present_event = threading.Event()
    present_event.set()
    
    day_event = threading.Event()
    day_event.set()
    
    start_thread(main_function, (commandqueue, statusqueue, present_event, day_event), False)
    
    start_thread(time_function, (commandqueue,), True)
    start_thread(bluetooth_function, (commandqueue, day_event), True)
    start_thread(temp_sensor_function, (commandqueue,), True)
    start_thread(light_sensor_function, (commandqueue, present_event, day_event), True)
    
    start_thread(http_commands.http_function, (commandqueue, statusqueue), True)
    start_thread(telegram_bot.bot_server_function, (telegramqueue,), True)
    
    commandqueue.join()
    statusqueue.join()
    telegramqueue.join()
