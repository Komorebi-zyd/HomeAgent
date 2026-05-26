### 客户端/HA平台的通信代码
from datetime import datetime,timezone
import socket
import json
import time

IP = "192.168.56.1"
PORT = 8080
BUFFER_SIZE = 1048576
COMMUNICATE_FLOW = [
    "get_entities",
    "get_entity_state",
    "time_now",
    "command",
    "finish",
]


def sock_recv(socket):
    data_recv = socket.recv(BUFFER_SIZE)
    if not data_recv:
        print("空消息")
        return None
    data_recv = json.loads(data_recv.decode("utf-8"))
    return data_recv


def sock_send(socket,message):
    data_send = json.dumps(message).encode("utf-8")
    socket.sendall(data_send)
    return None


def zyd_communicate(now_entity:dict,entities:list,entity_state:dict):
    cmds = []
    count = 0
    while True:
        
        try:
            # 创建一个 socket 对象
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # 连接到 A
            sock.connect((IP, PORT))
            print("连接成功",sock)
            break
        except socket.error:
            # 连接失败，等待重试
            if count == 3:
                print("连接失败，直接执行...")
                return None
            print("连接失败，重试中...")
            time.sleep(0.1)
            count += 1
            continue

    ################################ 发送消息 ################################
    data_send = json.dumps(now_entity).encode("utf-8")
    sock.sendall(data_send)

    while True:
        data_recv = sock_recv(sock)

        if data_recv is None:
            break

        elif data_recv["type"] == 0:
            message = {
                "type":0,  # 类型为0，表示获取设备列表。
                "entities":entities  # 返回所有实体。
            }
            sock_send(sock,message)

        elif data_recv["type"] == 1:
            entity_id = data_recv["entity_id"]
            if entity_id in entities:
                state_onlyread = entity_state[entity_id].as_dict()
                message = {
                    "type":1,  # 类型为1，表示获取设备列表。
                    "entity_id":entity_id,
                    "state":state_onlyread["state"],
                    "last_changed":state_onlyread["last_changed"],
                    "last_triggered":(state_onlyread["attributes"]["last_triggered"].strftime("%Y-%m-%dT%H:%M:%S.%f")+"+00:00" if state_onlyread["attributes"]["last_triggered"] is not None else None) if "last_triggered" in state_onlyread["attributes"] else None
                }
                sock_send(sock,message)
            else:
                message = {
                    "type":1,  # 类型为1，表示获取设备列表。
                    "entity_id":entity_id,
                    "state":None  # 返回实体的状态。
                }
                sock_send(sock,message)

        elif data_recv["type"] == 2:
            message = {
                "type":2,
                "time":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")+"+00:00"
            }
            sock_send(sock,message)

        elif data_recv["type"] == 3:
            cmds += data_recv["cmds"]
            message = {
                "type":3
            }
            sock_send(sock,message)

        elif data_recv["type"] == -1:
            message = {
                "type":-1,
            }
            sock_send(sock,message)
            break



    sock.close()  # 断开连接
    return cmds

if __name__ == "__main__":
    zyd_communicate()