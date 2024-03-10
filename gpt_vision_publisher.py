import argparse
import copy
import os
import sys
from concurrent import futures
from typing import Any

import cv2
import depthai as dai
import grpc
import numpy as np
from akari_chatgpt_bot.lib.chat_akari_grpc import ChatStreamAkariGrpc

sys.path.append(os.path.join(os.path.dirname(__file__), "lib/grpc"))
import gpt_server_pb2
import gpt_server_pb2_grpc
import voicevox_server_pb2
import voicevox_server_pb2_grpc

# OAK-D LITEの視野角
fov = 56.7


class GptServer(gpt_server_pb2_grpc.GptServerServiceServicer):
    """
    chatGPTにtextを送信し、返答をvoicevox_serverに送るgprcサーバ
    """

    def __init__(self, vision_model="gpt-4-vision-preview"):
        voicevox_channel = grpc.insecure_channel("localhost:10002")
        self.stub = voicevox_server_pb2_grpc.VoicevoxServerServiceStub(voicevox_channel)
        self.chat_stream_akari_grpc = ChatStreamAkariGrpc()
        content = "チャットボットとしてロールプレイします。あかりという名前のカメラロボットとして振る舞ってください。認識結果としてあなたから見た左右、上下、奥行きが渡されるので、それに基づいて回答してください。距離はセンチメートルで答えてください"
        self.messages = [
            self.chat_stream_akari_grpc.create_message(content, role="system")
        ]
        self.vision_model = vision_model

    def SetGpt(
        self, request: gpt_server_pb2.SetGptRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SetGptReply:
        response = ""
        if len(request.text) < 2:
            return gpt_server_pb2.SetGptReply(success=True)
        print(f"Receive: {request.text}")
        if request.is_finish:
            content = f"{request.text}。一文で簡潔に答えてください。"
        else:
            content = f"「{request.text}。"
        tmp_messages = copy.deepcopy(self.messages)
        if request.is_finish:
            tmp_messages.append(
                self.chat_stream_akari_grpc.create_vision_message(
                    content, self.frame, model=self.vision_model
                )
            )
        else:
            tmp_messages.append(self.chat_stream_akari_grpc.create_message(content))
        if request.is_finish:
            for sentence in self.chat_stream_akari_grpc.chat(
                tmp_messages, model=self.vision_model
            ):
                print(f"Send voicevox: {sentence}")
                self.stub.SetVoicevox(
                    voicevox_server_pb2.SetVoicevoxRequest(text=sentence)
                )
                response += sentence
            self.messages.append(
                self.chat_stream_akari_grpc.create_message(response, role="assistant")
            )
        else:
            for sentence in self.chat_stream_akari_grpc.chat_and_motion(tmp_messages):
                print(f"Send voicevox: {sentence}")
                self.stub.SetVoicevox(
                    voicevox_server_pb2.SetVoicevoxRequest(text=sentence)
                )
                response += sentence
        print("finish")
        return gpt_server_pb2.SetGptReply(success=True)

    def SendMotion(
        self, request: gpt_server_pb2.SendMotionRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SendMotionReply:
        success = self.chat_stream_akari_grpc.send_reserved_motion()
        return gpt_server_pb2.SendMotionReply(success=success)

    def update_frame(self, frame: np.ndarray) -> None:
        self.frame = frame


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
        default="gpt-4-vision-preview",
        type=str,
    )
    args = parser.parse_args()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
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
