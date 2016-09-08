#!/usr/bin/python

import socket  
import time
import fcntl
import re
import os
import errno
import struct
import argparse
import pickle
import time
from threading import Thread
from time import sleep
from collections import OrderedDict


#Configuration
file_name_search_bulbs = "search_bulbs"
time_between_broadcast_search_seconds = "30"
DEBUGGING = False
TIMEOUT = 1000


def debug(msg):
  if DEBUGGING:
    print msg

detected_bulbs = {}
bulb_idx2ip = {}
bulb_2execute = {}
RUNNING = True
current_command_id = 0
MCAST_GRP = '239.255.255.250'
effect_ms = 0
WARNING = ""
skip_search = False
error_json = False
error_string_json = ""
return_json_string = ""


scan_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) 
fcntl.fcntl(scan_socket, fcntl.F_SETFL, os.O_NONBLOCK)
listen_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
listen_socket.bind(("", 1982))
fcntl.fcntl(listen_socket, fcntl.F_SETFL, os.O_NONBLOCK)
mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
listen_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


parser = argparse.ArgumentParser()
parser.add_argument("-t", "--timeout", type=int, help="timeout for seeking lightbulb")
parser.add_argument("-l", "--list", help="list light bulbs on the network",action="store_true")
parser.add_argument("-to", "--toggle", help="list light bulbs on the network",action="store_true")
parser.add_argument("-b", "--bright", type=int, help="set brightness")
parser.add_argument("-r", "--rgb", type=int, help="set rgb")
parser.add_argument("-hue", "--hue", type=int, help="set hue-saturation")
parser.add_argument("-s", "--saturation", type=int, help="set hue-saturation")
parser.add_argument("-c", "--ctemp", type=int, help="set the color temperature")
parser.add_argument("-cra", "--cronadd", type=int, help="list light bulbs on the network")
parser.add_argument("-crg", "--cronget", type=int, help="list light bulbs on the network")
parser.add_argument("-crd", "--crondel", type=int, help="list light bulbs on the network")
parser.add_argument("-gp", "--getprop", help="list light bulbs on the network",action="store_true")
parser.add_argument("-e", "--effect", type=int, help="transition effect parameter in ms. by default : sudden")
parser.add_argument("-i", "--id", help="id of the light bulb fetchable by \"list\" command, can be used multiple time for multiple bulbs",action='append',)
parser.parse_args()
args = parser.parse_args()

def json_return(status,msg = ""):
  global error_json, error_string_json, return_json_string
  if status == 1:
    error_json = True
    if error_string_json == "":
      error_string_json = "[ \"" + msg + "\""
    else:
      error_string_json += ", \"" + msg + "\""
  elif status == 2:
    return_json_string = msg
  elif status == 3:
    if error_json:
      json_final = "{\"success\": false, \"error\": " + error_string_json + " ]}"
    else:
      json_final = "{\"success\": true, \"result\": " + return_json_string + "}"
    print json_final

if not args.list and not args.toggle and not args.bright and not args.rgb and not args.hue and not args.saturation and not args.ctemp and not args.cronadd and not args.crondel and not args.cronget:
  json_return(1, "no_valid_command")
  json_return(3)
  exit()

if args.id:
	bulb_2execute = args.id

if args.effect > 0:
  if args.effect >= 30:
    effect_ms = args.effect
  else:
    debug("effect_duration_bad_value")

if args.timeout:
  TIMEOUT = args.timeout

if os.path.isfile(file_name_search_bulbs):
  with open(file_name_search_bulbs, "rb") as f:
      data_search = pickle.load(f)
      if int(time.time()) < data_search[0]+time_between_broadcast_search_seconds:
        detected_bulbs = list(data_search[1])
        bulb_idx2ip = list(data_search[2])
        skip_search = True
        for i in range(1, len(detected_bulbs)+1):
          if args.id and i in bulb_2execute:
            execute_command(i)
            bulb_2execute.remove(i)
          elif not args.id:
            execute_command(i)

def next_cmd_id():
  global current_command_id
  current_command_id += 1
  return current_command_id
    
def send_search_broadcast():
  '''
  multicast search request to all hosts in LAN, do not wait for response
  '''
  multicase_address = (MCAST_GRP, 1982) 
  debug("send search request")
  msg = "M-SEARCH * HTTP/1.1\r\n" 
  msg = msg + "HOST: 239.255.255.250:1982\r\n"
  msg = msg + "MAN: \"ssdp:discover\"\r\n"
  msg = msg + "ST: wifi_bulb"
  scan_socket.sendto(msg, multicase_address)

def bulbs_detection_loop():
  '''
  a standalone thread broadcasting search request and listening on all responses
  '''
  global RUNNING
  debug("bulbs_detection_loop running")
  search_interval=30000
  read_interval=100
  time_elapsed=0

  while RUNNING:
    if time_elapsed%search_interval == 0:
      send_search_broadcast()

    # scanner
    while True:
      try:
        data = scan_socket.recv(2048)
      except socket.error, e:
        err = e.args[0]
        if err == errno.EAGAIN or err == errno.EWOULDBLOCK:
            break
        else:
            print e
            sys.exit(1)
      	handle_search_response(data)

    # passive listener 
    while True:
      try:
        data, addr = listen_socket.recvfrom(2048)
      except socket.error, e:
        err = e.args[0]
        if err == errno.EAGAIN or err == errno.EWOULDBLOCK:
            break
        else:
            print e
            sys.exit(1)
      handle_search_response(data)

    time_elapsed+=read_interval
    sleep(read_interval/1000.0)
    if time_elapsed >= TIMEOUT:
      RUNNING = False
      json_return(1, "timeout")
  scan_socket.close()
  listen_socket.close()

def get_param_value(data, param):
  '''
  match line of 'param = value'
  '''
  param_re = re.compile(param+":\s*([ -~]*)") #match all printable characters
  match = param_re.search(data)
  value=""
  if match != None:
    value = match.group(1)
    return value
    
def handle_search_response(data):
  '''
  Parse search response and extract all interested data.
  If new bulb is found, insert it into dictionary of managed bulbs. 
  '''
  location_re = re.compile("Location.*yeelight[^0-9]*([0-9]{1,3}(\.[0-9]{1,3}){3}):([0-9]*)")
  match = location_re.search(data)
  if match == None:
    debug( "invalid data received: " + data )
    return 

  host_ip = match.group(1)
  if detected_bulbs.has_key(host_ip):
    bulb_id = detected_bulbs[host_ip][0]
  else:
    bulb_id = len(detected_bulbs)+1
  host_port = match.group(3)
  model = get_param_value(data, "model")
  power = get_param_value(data, "power") 
  bright = get_param_value(data, "bright")
  rgb = get_param_value(data, "rgb")
  # use two dictionaries to store index->ip and ip->bulb map
  detected_bulbs[host_ip] = [bulb_id, model, power, bright, rgb, host_port]
  bulb_idx2ip[bulb_id] = host_ip
  # user interaction end, tell detection thread to quit and wait
  if args.id and bulb_id in bulb_2execute:
    execute_command(bulb_id)
    bulb_2execute.remove(bulb_id)
    if len(bulb_2execute) == 0:
      RUNNING = False
  elif not args.id:
    execute_command(bulb_id)

  with open(file_name_search_bulbs, "wb") as f:
    temp_array = [int(time.time()), detected_bulbs, bulb_idx2ip]
    pickle.dump(temp_array, f)


def display_bulb(idx, eol):
  if not bulb_idx2ip.has_key(idx):
    json_return(1, "invalid_idx")
    return
  bulb_ip = bulb_idx2ip[idx]
  model = detected_bulbs[bulb_ip][1]
  power = detected_bulbs[bulb_ip][2]
  bright = detected_bulbs[bulb_ip][3]
  rgb = detected_bulbs[bulb_ip][4]
  json = "\"" + str(idx) + "\": {\"ip\"=\"" \
    +bulb_ip + "\",\"model\": \"" + model \
    +"\",\"power\":\"" + power + "\",\"bright\":\"" \
    + bright + "\",\"rgb\":\"" + rgb + "\"}"
  if not eol:
    json += ","
  return json

def execute_command(idx):
  if args.list:
    display_bulbs()
  if args.toggle:
    operate_on_bulb(idx, "toggle", "", effect_ms)
  if args.bright:
    operate_on_bulb(idx, "set_bright", args.bright, effect_ms)
  if args.rgb:
    operate_on_bulb(idx, "set_rgb", args.rgb, effect_ms)
  if (args.hue and args.saturation):
    operate_on_bulb(idx, "set_hsv", args.hue + ", " + args.saturation, effect_ms)
  elif (args.hue and not args.saturation) or (args.saturation and not args.hue):
    json_return(1, "missing_parameter_for_hue")
  if args.ctemp:
    operate_on_bulb(idx, "set_ct_abx", args.ctemp, effect_ms)


def display_bulbs():
  json = "{\"bulbs\": " + str(len(detected_bulbs)) + ", \"bulb\": {"
  for i in range(1, len(detected_bulbs)+1):
    if(i == len(detected_bulbs)):
      json += display_bulb(i, True)
    else:
      json += display_bulb(i, False)
  json_return(2, json)

def operate_on_bulb(idx, method, params, effect):
  '''
  Operate on bulb; no gurantee of success.
  Input data 'params' must be a compiled into one string.
  E.g. params="1"; params="\"smooth\"", params="1,\"smooth\",80"
  '''
  if not bulb_idx2ip.has_key(idx):
    json_return(1, "invalid_idx")
    return
  
  #HERE EFFECT
  if effect > 0:
    params += ", \"smooth\"," + str(effect)

  bulb_ip=bulb_idx2ip[idx]
  port=detected_bulbs[bulb_ip][5]
  try:
    tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_socket.connect((bulb_ip, int(port)))
    msg="{\"id\":" + str(next_cmd_id()) + ",\"method\":\""
    msg += method + "\",\"params\":[" + params + "]}\r\n"
    tcp_socket.send(msg)
    tcp_socket.close()
  except Exception as e:
    json_return(1, e)

def handle_user_input():
  '''
  User interaction loop. 
  '''
  while True:
    command_line = raw_input("Enter a command: ")
    valid_cli=True
    debug("command_line=" + command_line)
    command_line.lower() # convert all user input to lower case, i.e. cli is caseless
    argv = command_line.split() # i.e. don't allow parameters with space characters
    if len(argv) == 0:
      continue
    if argv[0] == "q" or argv[0] == "quit":
      print "Bye!"
      return
    elif argv[0] == "l" or argv[0] == "list":
      display_bulbs()
    elif argv[0] == "r" or argv[0] == "refresh":
      detected_bulbs.clear()
      bulb_idx2ip.clear()
      send_search_broadcast()
      #sleep(0.5)
      #display_bulbs()
    elif argv[0] == "h" or argv[0] == "help":
      continue
    elif argv[0] == "t" or argv[0] == "toggle":
      if len(argv) != 2:
        valid_cli=False
      else:
        try:
          i = int(float(argv[1]))
          toggle_bulb(i)
        except:
          valid_cli=False
    elif argv[0] == "b" or argv[0] == "bright":
      if len(argv) != 3:
        print "incorrect argc"
        valid_cli=False
      else:
        try:
          idx = int(float(argv[1]))
          print "idx", idx
          bright = int(float(argv[2]))
          print "bright", bright
          set_bright(idx, bright)
        except:
          valid_cli=False
    else:
      valid_cli=False
          
    if not valid_cli:
      print "error: invalid command line:", command_line

## main starts here
# print welcome message first
# start the bulb detection thread
if not skip_search:
  detection_thread = Thread(target=bulbs_detection_loop)
  detection_thread.start()
  detection_thread.join()

json_return(3)
