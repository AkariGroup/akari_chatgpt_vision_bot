import argparse
import copy
import os
import sys
import threading
from concurrent import futures
from typing import Optional

import cv2
import grpc
import numpy as np
from akari_chatgpt_bot.lib.chat_akari_grpc import ChatStreamAkariGrpc
from lib.akari_yolo_lib.oakd_tracking_yolo import OakdTrackingYolo

sys.path.append(os.path.join(os.path.dirname(__file__), "lib/grpc"))
import gpt_server_pb2
import gpt_server_pb2_grpc
import voice_server_pb2
import voice_server_pb2_grpc

messages = []
chat_stream_akari_grpc = ChatStreamAkariGrpc()
voice_channel = grpc.insecure_channel("localhost:10002")
voice_stub = voice_server_pb2_grpc.VoiceServerServiceStub(voice_channel)

GREETING_DISTANCE = 2500  # この距離以内に人が来たら声がけする。


class GptServer(gpt_server_pb2_grpc.GptServerServiceServicer):
    """
    chatGPTにtextを送信し、返答をvoice_serverに送るgprcサーバ
    """

    def __init__(self):
        global messages
        content = "チャットボットとしてロールプレイします。あかりという名前のカメラロボットとして振る舞ってください。"
        messages = [chat_stream_akari_grpc.create_message(content, role="system")]

    def SetGpt(
        self, request: gpt_server_pb2.SetGptRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SetGptReply:
        global messages
        response = ""
        if len(request.text) < 2:
            return gpt_server_pb2.SetGptReply(success=True)
        print(f"Receive: {request.text}")
        if request.is_finish:
            content = f"{request.text}。回答は一文で短くまとめて答えてください。"
        else:
            content = f"{request.text}。"
        tmp_messages = copy.deepcopy(messages)
        tmp_messages.append(chat_stream_akari_grpc.create_message(content))
        if request.is_finish:
            messages = copy.deepcopy(tmp_messages)
            for sentence in chat_stream_akari_grpc.chat(
                tmp_messages, model="gpt-4-turbo"
            ):
                print(f"Send voice: {sentence}")
                try:
                    voice_stub.SetText(voice_server_pb2.SetTextRequest(text=sentence))
                except BaseException:
                    print("voice server send error")
                response += sentence
            messages.append(
                chat_stream_akari_grpc.create_message(response, role="assistant")
            )
        else:
            for sentence in chat_stream_akari_grpc.chat_and_motion(
                tmp_messages, short_response=True, model="gpt-4-turbo"
            ):
                print(f"Send voice: {sentence}")
                try:
                    voice_stub.SetText(voice_server_pb2.SetTextRequest(text=sentence))
                except BaseException:
                    print("voice server send error")
                response += sentence
        return gpt_server_pb2.SetGptReply(success=True)

    def SendMotion(
        self, request: gpt_server_pb2.SendMotionRequest(), context: grpc.ServicerContext
    ) -> gpt_server_pb2.SendMotionReply:
        success = chat_stream_akari_grpc.send_reserved_motion()
        return gpt_server_pb2.SendMotionReply(success=success)


def send_greeting_vision_message(frame: np.ndarray, model: str = "gpt-4-turbo") -> None:
    global messages
    text = "画像の人の容姿や年齢、服装を見て挨拶の声がけをしてください。簡潔に答えてください。"
    tmp_messages = copy.deepcopy(messages)
    tmp_messages.append(
        chat_stream_akari_grpc.create_vision_message(
            text=text,
            image=frame,
            model=model,
            image_width=frame.shape[1],
            image_height=frame.shape[0],
        )
    )
    # 会話履歴にはデータ削減のため画像抜きのデータを残す
    messages.append(chat_stream_akari_grpc.create_message(text))
    response = ""
    for sentence in chat_stream_akari_grpc.chat(tmp_messages, model=model):
        print(f"Send voice: {sentence}")
        try:
            voice_stub.SetVoicePlayFlg(
                voice_server_pb2.SetVoicePlayFlgRequest(flg=True)
            )
            voice_stub.SetText(voice_server_pb2.SetTextRequest(text=sentence))
        except BaseException:
            print("voice server send error")
        response += sentence
    messages.append(chat_stream_akari_grpc.create_message(response, role="assistant"))


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
                        and tracklet.spatialCoordinates.z <= GREETING_DISTANCE
                    ):
                        roi = tracklet.roi.denormalize(frame.shape[1], frame.shape[0])
                        x1 = int(roi.topLeft().x)
                        y1 = int(roi.topLeft().y)
                        x2 = int(roi.bottomRight().x)
                        y2 = int(roi.bottomRight().y)
                        person_frame = frame[y1:y2, x1:x2]
                        greeting_thread = threading.Thread(
                            target=send_greeting_vision_message, args=(person_frame,)
                        )
                        greeting_thread.start()
                        greeting_person_id = tracklet.id
                        break

        if frame is not None:
            oakd_tracking_yolo.display_frame("nn", frame, tracklets)
        if cv2.waitKey(1) == ord("q"):
            end = True
            break


if __name__ == "__main__":
    main()
