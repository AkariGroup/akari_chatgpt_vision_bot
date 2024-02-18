import argparse

import os
import sys
import openai
import grpc
import threading
from concurrent import futures
from typing import Any
from akari_chatgpt_bot.lib.chat import create_message
from akari_chatgpt_bot.lib.chat_akari_grpc import ChatStreamAkariGrpc
from akari_chatgpt_bot.lib.conf import OPENAI_APIKEY
import copy
import cv2
from lib.akari_yolo_lib.oakd_tracking_yolo import OakdTrackingYolo

sys.path.append(os.path.join(os.path.dirname(__file__), "lib/grpc"))
import gpt_server_pb2
import gpt_server_pb2_grpc
import voicevox_server_pb2
import voicevox_server_pb2_grpc

# OAK-D LITEの視野角
fov = 56.7


class YoloTracking(object):
    def __init__(
        self,
        config_path: str,
        model_path: str,
        fps: int,
        fov: float,
    ) -> None:
        self.oakd_tracking_yolo = OakdTrackingYolo(
            config_path=config_path, model_path=model_path, fps=fps, fov=fov
        )
        self.tracklets = []
        self.labels = self.oakd_tracking_yolo.get_labels()

    def set_tracklet(self, tracklets: Any) -> None:
        self.tracklets = tracklets

    def get_result_text(self) -> str:
        text = " 認識結果 {\n"
        if self.tracklets is not None:
            for tracklet in self.tracklets:
                if tracklet.status.name != "NEW" and tracklet.status.name != "TRACKED":
                    continue
                text += f"種類: {self.labels[tracklet.label]},"
                text += "あなたから見た位置:"
                if tracklet.spatialCoordinates.x >= 0:
                    text += "右"
                else:
                    text += "左"
                text += "{:.2f} m".format(abs(tracklet.spatialCoordinates.x) / 1000)
                if tracklet.spatialCoordinates.y >= 0:
                    text += "上"
                else:
                    text += "下"
                text += "{:.2f} m".format(abs(tracklet.spatialCoordinates.y) / 1000)
                text += "近さ {:.2f} m".format(
                    abs(tracklet.spatialCoordinates.z) / 1000
                )
                text += "\n"
        text += "}"
        return text


class GptServer(gpt_server_pb2_grpc.GptServerServiceServicer):
    """
    chatGPTにtextを送信し、返答をvoicevox_serverに送るgprcサーバ
    """

    def __init__(self, yolo_tracking: YoloTracking):
        content = "チャットボットとしてロールプレイします。あかりという名前のカメラロボットとして振る舞ってください。認識結果としてあなたから見た左右、上下、奥行きが渡されるので、それに基づいて回答してください。距離はセンチメートルで答えてください"
        self.messages = [create_message(content, role="system")]
        voicevox_channel = grpc.insecure_channel("localhost:10002")
        self.stub = voicevox_server_pb2_grpc.VoicevoxServerServiceStub(voicevox_channel)
        self.chat_stream_akari_grpc = ChatStreamAkariGrpc()
        self.yolo_tracking = yolo_tracking

    def SetGpt(
        self, request: gpt_server_pb2.SetGptRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SetGptReply:
        response = ""
        if len(request.text) < 2:
            return gpt_server_pb2.SetGptReply(success=True)
        print(f"Receive: {request.text}")
        if request.is_finish:
            request.text += self.yolo_tracking.get_result_text()
            content = f"{request.text}。一文で簡潔に答えてください。"
        else:
            content = f"「{request.text}」という文に対して、以下の「」内からどれか一つを選択して、それだけ回答してください。\n「えーと。」「はい。」「うーん。」「いいえ。」「はい、そうですね。」「そうですね…。」「いいえ、違います。」「こんにちは。」「ありがとうございます。」「なるほど。」「まあ。」"
        tmp_messages = copy.deepcopy(self.messages)
        tmp_messages.append(create_message(content))
        #if request.is_finish:
        #    self.messages = copy.deepcopy(tmp_messages)
        if request.is_finish:
            for sentence in self.chat_stream_akari_grpc.chat(tmp_messages):
                print(f"Send voicevox: {sentence}")
                self.stub.SetVoicevox(
                    voicevox_server_pb2.SetVoicevoxRequest(text=sentence)
                )
                self.messages.append(create_message(response, role="assistant"))
                response += sentence
        else:
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
        success = self.chat_stream_akari_grpc.send_motion()
        return gpt_server_pb2.SendMotionReply(success=success)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-m",
        "--model",
        help="Provide model name or model path for inference",
        default="yolov4_tiny_coco_416x416",
        type=str,
    )
    parser.add_argument(
        "-c",
        "--config",
        help="Provide config path for inference",
        default="json/yolov4-tiny.json",
        type=str,
    )
    parser.add_argument(
        "-f",
        "--fps",
        help="Camera frame fps. This should be smaller than nn inference fps",
        default=8,
        type=int,
    )
    parser.add_argument(
        "--ip", help="Gpt server ip address", default="127.0.0.1", type=str
    )
    parser.add_argument(
        "--port", help="Gpt server port number", default="10001", type=str
    )
    args = parser.parse_args()
    yolo_tracking = YoloTracking(
        config_path=args.config,
        model_path=args.model,
        fps=args.fps,
        fov=fov,
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    gpt_server_pb2_grpc.add_GptServerServiceServicer_to_server(
        GptServer(yolo_tracking), server
    )
    server.add_insecure_port(args.ip + ":" + args.port)
    server.start()
    print(f"gpt_publisher start. port: {args.port}")
    while True:
        frame = None
        detections = []
        try:
            frame, detections, tracklets = yolo_tracking.oakd_tracking_yolo.get_frame()
        except BaseException:
            pass
        if tracklets is not None:
            yolo_tracking.set_tracklet(tracklets)
        if frame is not None:
            yolo_tracking.oakd_tracking_yolo.display_frame(
                "nn", frame, yolo_tracking.tracklets
            )
        if cv2.waitKey(1) == ord("q"):
            end = True
            break


if __name__ == "__main__":
    main()
