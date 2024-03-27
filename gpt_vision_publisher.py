import argparse
import copy
import os
import sys
from concurrent import futures
from gpt_stream_parser import force_parse_json
from distutils.util import strtobool

import cv2
import depthai as dai
import grpc
import json
import numpy as np
from akari_chatgpt_bot.lib.chat_akari_grpc import ChatStreamAkariGrpc

sys.path.append(os.path.join(os.path.dirname(__file__), "lib/grpc"))
import gpt_server_pb2
import gpt_server_pb2_grpc
import voicevox_server_pb2
import voicevox_server_pb2_grpc
import motion_server_pb2
import motion_server_pb2_grpc

# OAK-D LITEの視野角
fov = 56.7


class GptServer(gpt_server_pb2_grpc.GptServerServiceServicer):
    """
    chatGPTにtextを送信し、返答をvoicevox_serverに送るgprcサーバ
    """

    def __init__(self, vision_model="claude-3-sonnet-20240229"):
        voicevox_channel = grpc.insecure_channel("localhost:10002")
        self.stub = voicevox_server_pb2_grpc.VoicevoxServerServiceStub(voicevox_channel)
        self.chat_stream_akari_grpc = ChatStreamAkariGrpc()
        content = "チャットボットとしてロールプレイします。あかりという名前のカメラロボットとして振る舞ってください。"
        self.messages = [
            self.chat_stream_akari_grpc.create_message(content, role="system")
        ]
        self.vision_model = vision_model

    def SetGpt(
        self, request: gpt_server_pb2.SetGptRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SetGptReply:
        response = ""
        is_finish = True
        if request.HasField("is_finish"):
            is_finish = request.is_finish
        if len(request.text) < 2:
            return gpt_server_pb2.SetGptReply(success=True)
        print(f"Receive: {request.text}")
        if is_finish:
            content = f"{request.text}。一文で簡潔に答えてください。"
        else:
            content = f"「{request.text}。"
        tmp_messages = copy.deepcopy(self.messages)
        if is_finish:
            tmp_messages.append(
                self.chat_stream_akari_grpc.create_vision_message(
                    content, self.frame, model=self.vision_model
                )
            )
            for sentence in self.chat_stream_akari_grpc.chat(
                tmp_messages, model=self.vision_model
            ):
                print(f"Send voicevox: {sentence}")
                self.stub.SetVoicevox(
                    voicevox_server_pb2.SetVoicevoxRequest(text=sentence)
                )
                response += sentence
        else:
            tmp_messages.append(self.chat_stream_akari_grpc.create_message(content))
            for sentence in self.chat_stream_akari_grpc.chat_and_motion(tmp_messages):
                print(f"Send voicevox: {sentence}")
                self.stub.SetVoicevox(
                    voicevox_server_pb2.SetVoicevoxRequest(text=sentence)
                )
                response += sentence
        return gpt_server_pb2.SetGptReply(success=True)

    def SendMotion(
        self, request: gpt_server_pb2.SendMotionRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SendMotionReply:
        success = self.chat_stream_akari_grpc.send_reserved_motion()
        return gpt_server_pb2.SendMotionReply(success=success)

    def update_frame(self, frame: np.ndarray) -> None:
        self.frame = frame


class SelectiveGptServer(GptServer):
    def selective_vision_chat_anthropic(
        self, messages, content, frame, temperature=0.7
    ) -> str:
        response = ""
        use_vision = False
        judge_messages = copy.deepcopy(messages)
        judge_content = f"「{content}」に対して、画像を見て回答するか、見ないで回答するかを決定し、下記のJSON形式で出力して下さい。{{\"vision\": \"画像を見る場合は\"True\"、見ない場合は\"False\"\", \"talk\": \"画像を見る場合は空白、見ない場合は回答のテキストを出力\"}}"
        judge_message = self.chat_stream_akari_grpc.create_message(judge_content)
        judge_messages.append(judge_message)

        system_message = ""
        user_messages = []
        for message in judge_messages:
            if message["role"] == "system":
                system_message = message["content"]
            else:
                user_messages.append(message)

        # Visionを使うかどうか判定。使わない場合はそのまま発話
        with self.chat_stream_akari_grpc.anthropic_client.messages.stream(
            model=self.vision_model,
            max_tokens=1000,
            temperature=temperature,
            messages=user_messages,
            system=system_message,
        ) as result:
            full_response = ""
            real_time_response = ""
            sentence_index = 0
            for text in result.text_stream:
                if text is None:
                    pass
                else:
                    full_response += text
                    real_time_response += text
                    try:
                        data_json = json.loads(full_response)
                        found_last_char = False
                        for char in self.chat_stream_akari_grpc.last_char:
                            if real_time_response[-1].find(char) >= 0:
                                found_last_char = True
                        if not found_last_char:
                            data_json["talk"] = data_json["talk"] + "。"
                    except BaseException:
                        data_json = force_parse_json(full_response)
                    if data_json is not None:
                        if "vision" in data_json:
                            print(data_json)
                            if strtobool(data_json["vision"]):
                                use_vision = True
                            if "talk" in data_json:
                                real_time_response = str(data_json["talk"])
                                for char in self.chat_stream_akari_grpc.last_char:
                                    pos = real_time_response[sentence_index:].find(char)
                                    if pos >= 0:
                                        sentence = real_time_response[
                                            sentence_index : sentence_index + pos + 1
                                        ]
                                        sentence_index += pos + 1
                                        response += sentence
                                        if not use_vision:
                                            self.chat_stream_akari_grpc.send_reserved_motion()
                                            print(f"Send voicevox: {sentence}")
                                            self.stub.SetVoicevox(
                                                voicevox_server_pb2.SetVoicevoxRequest(
                                                    text=sentence
                                                )
                                            )
        print(full_response)
        if use_vision:
            self.stub.SetVoicevox(voicevox_server_pb2.SetVoicevoxRequest(text="えーと"))
            try:
                self.chat_stream_akari_grpc.motion_stub.SetMotion(
                    motion_server_pb2.SetMotionRequest(
                        name="lookup", priority=3, repeat=False, clear=True
                    )
                )
            except BaseException:
                print("send error!")
                pass
            print("use_vision")
            # Visionを使う場合は再度質問
            vision_messages = copy.deepcopy(messages)
            vision_message = self.chat_stream_akari_grpc.create_vision_message(
                text=content, image=frame, model=self.vision_model
            )
            vision_messages.append(vision_message)
            response = ""
            system_message = ""
            user_messages = []
            for message in vision_messages:
                if message["role"] == "system":
                    system_message = message["content"]
                else:
                    user_messages.append(message)
            print(vision_messages)
            for sentence in self.chat_stream_akari_grpc.chat_and_motion(
                messages=vision_messages, model=self.vision_model
            ):
                print(f"Send voicevox: {sentence}")
                self.stub.SetVoicevox(
                    voicevox_server_pb2.SetVoicevoxRequest(text=sentence)
                )
                response += sentence
        return response

    def SetGpt(
        self, request: gpt_server_pb2.SetGptRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SetGptReply:
        response = ""
        is_finish = True
        if request.HasField("is_finish"):
            is_finish = request.is_finish
        if len(request.text) < 2:
            return gpt_server_pb2.SetGptReply(success=True)
        print(f"Receive: {request.text}")
        if is_finish:
            content = f"{request.text}。一文で簡潔に答えてください。"
        else:
            content = f"「{request.text}。"
        tmp_messages = copy.deepcopy(self.messages)
        if is_finish:
            response += self.selective_vision_chat_anthropic(
                tmp_messages,
                content,
                self.frame,
            )
        else:
            tmp_messages.append(self.chat_stream_akari_grpc.create_message(content))
            for sentence in self.chat_stream_akari_grpc.chat_and_motion(tmp_messages,model="claude-3-haiku-20240307"):
                response += sentence
        return gpt_server_pb2.SetGptReply(success=True)

    def SendMotion(
        self, request: gpt_server_pb2.SendMotionRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SendMotionReply:
        '''音声認識からの送信司令は無視する。
        '''
        return gpt_server_pb2.SendMotionReply(success=True)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ip", help="Gpt server ip address", default="127.0.0.1", type=str
    )
    parser.add_argument(
        "--port", help="Gpt server port number", default="10001", type=str
    )
    parser.add_argument(
        "-v",
        "--vision_model",
        help="LLM model name for vision",
        default="claude-3-haiku-20240307",
        type=str,
    )
    parser.add_argument(
        "--selective",
        help="Use selective vision bot",
        action="store_true",
    )
    args = parser.parse_args()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    if args.selective:
        gpt_server = SelectiveGptServer()
    else:
        gpt_server = GptServer()
    gpt_server_pb2_grpc.add_GptServerServiceServicer_to_server(gpt_server, server)
    server.add_insecure_port(args.ip + ":" + args.port)
    server.start()
    # OAK-Dのパイプライン作成
    pipeline = dai.Pipeline()
    cam_rgb = pipeline.create(dai.node.ColorCamera)
    xout_video = pipeline.create(dai.node.XLinkOut)
    cam_rgb.setBoardSocket(dai.CameraBoardSocket.RGB)
    cam_rgb.setResolution(dai.ColorCameraProperties.SensorResolution.THE_1080_P)
    cam_rgb.setVideoSize(1920, 1080)
    cam_rgb.setFps(10)
    cam_rgb.video.link(xout_video.input)
    xout_video.input.setBlocking(False)
    xout_video.input.setQueueSize(1)
    xout_video.setStreamName("video")

    print(f"gpt_publisher start. port: {args.port}")
    while True:
        frame = None
        with dai.Device(pipeline) as device:
            video = device.getOutputQueue(name="video", maxSize=1, blocking=False)  # type: ignore
            while True:
                videoIn = video.get()
                frame = videoIn.getCvFrame()
                if frame is not None:
                    gpt_server.update_frame(frame)
                    cv2.imshow("video", cv2.resize(frame, (640, 360)))
                if cv2.waitKey(1) == ord("q"):
                    break
            device.close()


if __name__ == "__main__":
    main()
