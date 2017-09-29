import base64
import json
import io
import os
import re
import threading
import wave
import zlib

import cherrypy
import numpy as np

from service import LabelService, TrainingService
from service import encode_audio, stride

def json_in(f):
    def merge_dicts(x, y):
        z = x.copy()
        z.update(y)
        return z
    def wrapper(*args, **kwargs):
        cl = cherrypy.request.headers["Content-Length"]
        data = json.loads(cherrypy.request.body.read(int(cl)).decode("utf-8"))
        kwargs = merge_dicts(kwargs, data)
        return f(*args, **kwargs)
    return wrapper

class TrainEndpoint(object):
    exposed = True
    def __init__(self, train_service):
        self.train_service = train_service

    @cherrypy.tools.json_out()
    def POST(self):
        return dict(success=self.train_service.run_train_script())

    @cherrypy.tools.json_out()
    def GET(self):
        return dict(in_progress=self.train_service.script_running)

class DataEndpoint(object):
    exposed = True
    def __init__(self, train_service):
        self.train_service = train_service

    @cherrypy.tools.json_out()
    @json_in
    def POST(self, **kwargs):
        wav_data = zlib.decompress(base64.b64decode(kwargs["wav_data"]))
        positive = kwargs["positive"]
        self.train_service.write_example(wav_data, positive=positive)
        success = dict(success=True)
        if not positive:
            return success
        neg_examples = self.train_service.generate_contrastive(wav_data)
        if not neg_examples:
            return success
        for example in neg_examples:
            self.train_service.write_example(example.byte_data, positive=False)
        return success

    @cherrypy.tools.json_out()
    def DELETE(self):
        self.train_service.clear_examples(positive=True)
        self.train_service.clear_examples(positive=False)
        return dict(success=True)

class ListenEndpoint(object):
    exposed = True
    def __init__(self, label_service, stride_size=500, min_keyword_prob=0., keyword="command"):
        """The REST API endpoint that determines if audio contains the keyword.

        Args:
            label_service: The labelling service to use
            stride_size: The stride in milliseconds of the 1-second window to use. It should divide 1000 ms.
            min_keyword_prob: The minimum probability the keyword must take in order to be classified as such
            keyword: The keyword
        """
        self.label_service = label_service
        self.stride_size = stride_size
        self.min_keyword_prob = min_keyword_prob
        self.keyword = keyword

    @cherrypy.tools.json_out()
    @json_in
    def POST(self, **kwargs):
        wav_data = zlib.decompress(base64.b64decode(kwargs["wav_data"]))
        for data in stride(wav_data, int(2 * 16000 * self.stride_size / 1000), 2 * 16000):
            label, prob = self.label_service.label(encode_audio(data))
            if label == "command" and prob >= self.min_keyword_prob:
                return dict(contains_command=True)
        return dict(contains_command=False)

def make_abspath(rel_path):
    if not os.path.isabs(rel_path):
        rel_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), rel_path)
    return rel_path

def start(config):
    cherrypy.config.update({
        "environment": "production",
        "log.screen": True
    })
    cherrypy.config.update(config["server"])
    rest_config = {"/": {
        "request.dispatch": cherrypy.dispatch.MethodDispatcher()
    }}
    model_path = make_abspath(config["model_path"])
    scripts_path = make_abspath(config["scripts_path"])
    speech_dataset_path = make_abspath(config["speech_dataset_path"])

    lbl_service = LabelService(model_path)
    train_service = TrainingService(scripts_path, speech_dataset_path, config["model_options"])
    cherrypy.tree.mount(ListenEndpoint(lbl_service), "/listen", rest_config)
    cherrypy.tree.mount(DataEndpoint(train_service), "/data", rest_config)
    cherrypy.tree.mount(TrainEndpoint(train_service), "/train", rest_config)
    cherrypy.engine.start()
    cherrypy.engine.block()