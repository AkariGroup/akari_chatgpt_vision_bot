import argparse
import copy
import os
import sys
import threading
from concurrent import futures
from typing import Any,Optional

import cv2
import grpc
import openai
from akari_chatgpt_bot.lib.chat_akari_grpc import ChatStreamAkariGrpc
from lib.akari_yolo_lib.oakd_tracking_yolo import OakdTrackingYolo

sys.path.append(os.path.join(os.path.dirname(__file__), "lib/grpc"))
import gpt_server_pb2
import gpt_server_pb2_grpc
import voicevox_server_pb2
import voicevox_server_pb2_grpc

# OAK-D LITEの視野角
fov = 56.7


class YoloGreetCheck(object):
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
                text += "{:.2f}メートル".format(
                    abs(tracklet.spatialCoordinates.x) / 1000
                )
                if tracklet.spatialCoordinates.y >= 0:
                    text += "上"
                else:
                    text += "下"
                text += "{:.2f}メートル".format(
                    abs(tracklet.spatialCoordinates.y) / 1000
                )
                text += "近さ {:.2f}メートル".format(
                    abs(tracklet.spatialCoordinates.z) / 1000
                )
                text += "\n"
        text += "}"
        return text


class GptServer(gpt_server_pb2_grpc.GptServerServiceServicer):
    """
    chatGPTにtextを送信し、返答をvoicevox_serverに送るgprcサーバ
    """

    def __init__(self):
        voicevox_channel = grpc.insecure_channel("localhost:10002")
        self.stub = voicevox_server_pb2_grpc.VoicevoxServerServiceStub(voicevox_channel)
        self.chat_stream_akari_grpc = ChatStreamAkariGrpc()
        content = "チャットボットとしてロールプレイします。あかりという名前のカメラロボットとして振る舞ってください。物体の認識結果は、カメラロボットであるあなたから見た距離です。質問の内容によっては回答に使ってください。"
        self.messages = [
            self.chat_stream_akari_grpc.create_message(content, role="system")
        ]

    def SetGpt(
        self, request: gpt_server_pb2.SetGptRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SetGptReply:
        response = ""
        if len(request.text) < 2:
            return gpt_server_pb2.SetGptReply(success=True)
        print(f"Receive: {request.text}")
        if request.is_finish:
            content = f"{request.text}。回答は一文で短くまとめて答えてください。"
        else:
            content = f"{request.text}。"
        tmp_messages = copy.deepcopy(self.messages)
        tmp_messages.append(self.chat_stream_akari_grpc.create_message(content))
        if request.is_finish:
            self.messages = copy.deepcopy(tmp_messages)
            for sentence in self.chat_stream_akari_grpc.chat(
                tmp_messages, model="gpt-4-turbo"
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
            for sentence in self.chat_stream_akari_grpc.chat_and_motion(
                tmp_messages, short_response=True
            ):
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-m",
        "--model",
        help="Provide model name or model path for inference",
        default="yolov7tiny_coco_416x416",
        type=str,
    )
    parser.add_argument(
        "-c",
        "--config",
        help="Provide config path for inference",
        default="json/yolov7tiny_coco_416x416.json",
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
        "-r",
        "--robot_coordinate",
        help="Convert object pos from camera coordinate to robot coordinate",
        action="store_true",
    )
    parser.add_argument(
        "--ip", help="Gpt server ip address", default="127.0.0.1", type=str
    )
    parser.add_argument(
        "--port", help="Gpt server port number", default="10001", type=str
    )
    args = parser.parse_args()
    oakd_tracking_yolo = OakdTrackingYolo(
        config_path=args.config,
        model_path=args.model,
        fps=args.fps,
        cam_debug=False,
        robot_coordinate=args.robot_coordinate,
        track_targets=["person"],
        show_bird_frame=True,
        show_spatial_frame=False,
        show_orbit=False,
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    gpt_server_pb2_grpc.add_GptServerServiceServicer_to_server(GptServer(), server)
    server.add_insecure_port(args.ip + ":" + args.port)
    server.start()
    print(f"gpt_publisher start. port: {args.port}")
    greeting_person_id: Optional[int] = None
    end = False
    while not end:
        frame = None
        detections = []
        try:
            frame, detections, tracklets = oakd_tracking_yolo.get_frame()
        except BaseException:
            pass
        if tracklets is not None:
            tracking = False
            if greeting_person_id is not None:
                for tracklet in tracklets:
                    if tracklet.id == greeting_person_id:
                        tracking = True
                if not tracking:
                    greeting_person_id = None
            else:
                for tracklet in tracklets:
                    if (
                        tracklet.status.name == "TRACKED"
                        and tracklet.spatialCoordinates.z <= 2000
                    ):
                        roi = tracklet.roi.denormalize(frame.shape[1], frame.shape[0])
                        x1 = int(roi.topLeft().x)
                        y1 = int(roi.topLeft().y)
                        x2 = int(roi.bottomRight().x)
                        y2 = int(roi.bottomRight().y)
                        person_frame = frame[y1:y2, x1:x2]
                        cv2.imshow("person", person_frame)
                        greeting_person_id = tracklet.id
                        break

        if frame is not None:
            oakd_tracking_yolo.display_frame("nn", frame, tracklets)
        if cv2.waitKey(1) == ord("q"):
            end = True
            break


if __name__ == "__main__":
    main()
