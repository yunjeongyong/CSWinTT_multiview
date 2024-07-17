import importlib
import os
import matplotlib.pyplot as plt
from collections import OrderedDict
from lib.test.evaluation.environment import env_settings
import time
import cv2
import csv
from lib.utils.lmdb_utils import decode_img
from pathlib import Path
import numpy as np
import time
from collections import deque
from kalman_filter import kalman_filter, P_0

"""Run the tracker with the focal sequence."""
from imageDataloader import Load2DFolder
# from FocalDataloader import LoadFocalFolder
from config_parser import ConfigParser
import torch
from datetime import datetime
import sys
# from plenoptic_tracker import PlenopticTracker
from lib.test.evaluation.plenoptic_tracker import PlenopticTracker
from copy import deepcopy
from enum import Enum


def csv_write(save_path, bbox, save_name):
    save_path.mkdir(parents=True, exist_ok=True)
    file_path = save_path / f'{save_name}.csv'
    f = open(file_path, 'a', newline='')
    wr = csv.writer(f)
    wr.writerow(bbox)
    f.close()

def csv_kalman_write(save_path, cam_num, x_meas, y_meas, x_esti, y_esti, save_name):
    file_path = save_path / f'{save_name}.csv'
    f = open(file_path, 'a', newline='')
    wr = csv.writer(f)
    # wr.writerow(bbox)
    print("csv_kalman_write", cam_num, x_meas, y_meas, x_esti, y_esti)
    wr.writerow([cam_num, x_meas, y_meas, x_esti, y_esti])
    f.close()

def trackerlist(name: str, parameter_name: str, dataset_name: str, run_ids = None, display_name: str = None,
                result_only=False):
    """Generate list of trackers.
    args:
        name: Name of tracking method.
        parameter_name: Name of parameter file.
        run_ids: A single or list of run_ids.
        display_name: Name to be displayed in the result plots.
    """
    if run_ids is None or isinstance(run_ids, int):
        run_ids = [run_ids]
    return [Tracker(name, parameter_name, dataset_name, run_id, display_name, result_only) for run_id in run_ids]


class TrackerItem:
    def __init__(self, buffer_size: int):
        self.new_bbox = None
        self.new_frame_idx = 1
        self.plenoptic_done = False

        self.tracker = []
        self.buffer_size = buffer_size
        self.plenoptic_before_diff = self.buffer_size // 2
        self.plenoptic_after_diff = self.buffer_size
        self.buffers: list[deque] = []
        self.score_buffers: list[deque] = []
        self.distance_avg_size = 5
        self.delta_buffer: list[deque] = []
        self.increasing = True


class PlenOpticState(Enum):
    NEED_START = 1
    RUNNING = 2
    NOT_RUNNING = 3


class Tracker:
    """Wraps the tracker for evaluation and running purposes.
    args:
        name: Name of tracking method.
        parameter_name: Name of parameter file.
        run_id: The run id.
        display_name: Name to be displayed in the result plots.
    """

    def __init__(self, name: str, parameter_name: str, dataset_name: str, run_id: int = None, display_name: str = None,
                 result_only=False):
        assert run_id is None or isinstance(run_id, int)

        self.name = name
        self.parameter_name = parameter_name
        self.dataset_name = dataset_name
        self.run_id = run_id
        self.display_name = display_name
        self.delta_threshold = 200
        self.new_bbox = []
        self.new_frame_idx = -1
        self.plenoptic_done = False
        self.plenoptic_running: list[PlenopticTracker] = []

        self.buffer_size = 10
        self.plenoptic_before_diff = self.buffer_size // 2
        self.plenoptic_after_diff = self.buffer_size
        self.bbox_buffers: list[list[deque]] = []
        self.score_buffers: list[list[deque]] = []
        self.distance_avg_size = 5
        self.delta_buffer: list[list[deque]] = []
        self.increasing = True
        self.tracker_items: list[TrackerItem] = []
        self.x_0_list = {}
        self.x_esti_list = {}
        self.P_list = {}

        self.xpos_meas_save = []
        self.ypos_meas_save = []
        self.xpos_esti_save = []
        self.ypos_esti_save = []
        self.kalman_gain_save = []

        config = ConfigParser('./config.json')
        video_name = config['video_name']
        start_frame_num = config['start_frame_num']
        last_frame_num = config['last_frame_num']
        start_focal_num = config['start_focal_num']
        last_focal_num = config['last_focal_num']
        plenoptic_video_name = "D:\\newvideo1_001_300"

        env = env_settings()
        if self.run_id is None:
            self.results_dir = '{}/{}/{}'.format(env.results_path, self.name, self.parameter_name)
        else:
            self.results_dir = '{}/{}/{}_{:03d}'.format(env.results_path, self.name, self.parameter_name, self.run_id)
        if result_only:
            self.results_dir = '{}/{}/{}'.format(env.results_path, "LaSOT", self.name)

        tracker_module_abspath = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                              '..', 'tracker', '%s.py' % self.name))
        if os.path.isfile(tracker_module_abspath):
            tracker_module = importlib.import_module('lib.test.tracker.{}'.format(self.name))
            self.tracker_class = tracker_module.get_tracker_class()
        else:
            self.tracker_class = None

    def create_tracker(self, params):
        tracker = self.tracker_class(params, self.dataset_name)
        return tracker

    def run_sequence(self, seq, debug=None):
        """Run tracker on sequence.
        args:
            seq: Sequence to run the tracker on.
            visualization: Set visualization flag (None means default value specified in the parameters).
            debug: Set debug level (None means default value specified in the parameters).
            multiobj_mode: Which mode to use for multiple objects.
        """
        params = self.get_parameters()

        debug_ = debug
        if debug is None:
            debug_ = getattr(params, 'debug', 0)

        params.debug = debug_

        # Get init information
        init_info = seq.init_info()

        tracker = self.create_tracker(params)

        output = self._track_sequence(tracker, seq, init_info)
        return output

    def _track_sequence(self, tracker, seq, init_info):

        output = {'target_bbox': [],
                  'time': []}
        if tracker.params.save_all_boxes:
            output['all_boxes'] = []
            output['all_scores'] = []

        def _store_outputs(tracker_out: dict, defaults=None):
            defaults = {} if defaults is None else defaults
            for key in output.keys():
                val = tracker_out.get(key, defaults.get(key, None))
                if key in tracker_out or val is not None:
                    output[key].append(val)

        # Initialize
        image = self._read_image(seq.frames[0])

        start_time = time.time()
        out = tracker.initialize(image, init_info)
        if out is None:
            out = {}

        prev_output = OrderedDict(out)
        init_default = {'target_bbox': init_info.get('init_bbox'),
                        'time': time.time() - start_time}
        if tracker.params.save_all_boxes:
            init_default['all_boxes'] = out['all_boxes']
            init_default['all_scores'] = out['all_scores']

        _store_outputs(out, init_default)

        for frame_num, frame_path in enumerate(seq.frames[1:], start=1):
            image = self._read_image(frame_path)
            if frame_num == 80:
                print()

            start_time = time.time()

            info = seq.frame_info(frame_num)
            info['previous_output'] = prev_output

            out = tracker.track(image, info)
            prev_output = OrderedDict(out)
            _store_outputs(out, {'time': time.time() - start_time})

        for key in ['target_bbox', 'all_boxes', 'all_scores']:
            if key in output and len(output[key]) <= 1:
                output.pop(key)

        return output

    def run_video(self, videofilepath, optional_box=None, debug=None, visdom_info=None, save_results=False):
        """Run the tracker with the vieofile.
        args:
            debug: Debug level.
        """
        params = self.get_parameters()

        debug_ = debug
        if debug is None:
            debug_ = getattr(params, 'debug', 0)
        params.debug = debug_

        params.tracker_name = self.name
        params.param_name = self.parameter_name

        multiobj_mode = getattr(params, 'multiobj_mode', getattr(self.tracker_class, 'multiobj_mode', 'default'))

        if multiobj_mode == 'default':
            tracker = self.create_tracker(params)

        elif multiobj_mode == 'parallel':
            tracker = MultiObjectWrapper(self.tracker_class, params, self.visdom, fast_load=True)
        else:
            raise ValueError('Unknown multi object mode {}'.format(multiobj_mode))

        assert os.path.isfile(videofilepath), "Invalid param {}".format(videofilepath)
        ", videofilepath must be a valid videofile"

        output_boxes = []

        cap = cv2.VideoCapture(videofilepath)
        display_name = 'Display: ' + tracker.params.tracker_name
        success, frame = cap.read()

        def _build_init_info(box):
            return {'init_bbox': box}

        if success is not True:
            print("Read frame from {} failed.".format(videofilepath))
            exit(-1)
        if optional_box is not None:
            assert isinstance(optional_box, (list, tuple))
            assert len(optional_box) == 4, "valid box's foramt is [x,y,w,h]"
            tracker.initialize(frame, _build_init_info(optional_box))
            output_boxes.append(optional_box)
        else:
            while True:
                frame_disp = frame.copy()

                cv.putText(frame_disp, 'Select target ROI and press ENTER', (20, 30), cv.FONT_HERSHEY_COMPLEX_SMALL,
                           1.5, (0, 0, 0), 1)

                x, y, w, h = cv2.selectROI(display_name, frame_disp, fromCenter=False)
                init_state = [x, y, w, h]
                tracker.initialize(frame, _build_init_info(init_state))
                output_boxes.append(init_state)
                break
        i = 0
        while True:
            i += 1
            print(f'{i}th frame start!')
            ret, frame = cap.read()

            if frame is None:
                break

            frame_disp = frame.copy()

            # Draw box
            out = tracker.track(frame)
            state = [int(s) for s in out['target_bbox']]
            output_boxes.append(state)

            cv2.rectangle(frame_disp, (state[0], state[1]), (state[2] + state[0], state[3] + state[1]),
                         (0, 255, 0), 5)

            font_color = (0, 0, 0)
            cv2.putText(frame_disp, 'Tracking!', (20, 30), cv.FONT_HERSHEY_COMPLEX_SMALL, 1,
                       font_color, 1)
            cv2.putText(frame_disp, 'Press r to reset', (20, 55), cv.FONT_HERSHEY_COMPLEX_SMALL, 1,
                       font_color, 1)
            cv2.putText(frame_disp, 'Press q to quit', (20, 80), cv.FONT_HERSHEY_COMPLEX_SMALL, 1,
                       font_color, 1)
            cv2.imwrite(f'result/{i:03d}.png', frame_disp)

        cap.release()
        cv2.destroyAllWindows()

        if save_results:
            if not os.path.exists(self.results_dir):
                os.makedirs(self.results_dir)
            video_name = Path(videofilepath).stem
            base_results_path = os.path.join(self.results_dir, 'video_{}'.format(video_name))

            tracked_bb = np.array(output_boxes).astype(int)
            bbox_file = '{}.txt'.format(base_results_path)
            np.savetxt(bbox_file, tracked_bb, delimiter='\t', fmt='%d')

    def draw_bboxes(self, img, tracker_num, bbox, color, identities=None, offset=(0, 0)):
        x, y, w, h = bbox
        label = str(tracker_num)
        t_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_PLAIN, 2, 2)[0]  # Nonvideo3
        print(img)
        print('x', x)
        print('y', y)
        print('h', h)
        print('w', w)
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 3)  # Nonvideo3
        cv2.rectangle(img, (x, y), (x + t_size[0], y + t_size[1]), color, -1)
        cv2.putText(img, label, (x, y + t_size[1]), cv2.FONT_HERSHEY_PLAIN, 2, [255, 255, 255], 2)  # Nonvideo3
        return img

    def _bbox_to_coord(self, bbox):
        x, y, w, h = bbox
        v = np.random.normal(0, 15)
        return x + v, y + v, np.array([x + w // 2 + v, y + h // 2 + v])

    def _save_buffer(self, k, bbox, score, cam: int):

        if cam == 16:
            self.bbox_buffers[k][cam].append(bbox)
            return
        buffer = self.bbox_buffers[k][cam]
        score_buffer = self.score_buffers[k][cam]
        delta_buffer = self.delta_buffer[k][cam]

        if len(buffer) >= self.buffer_size:
            buffer.popleft()
            score_buffer.popleft()
        if len(delta_buffer) >= self.distance_avg_size:
            delta_buffer.popleft()
        buffer.append(bbox)
        score_buffer.append(score)

    def _distance(self, k: int, n: int, frame_idx: int, bbox, num_cam) -> bool:
        buffer = self.bbox_buffers[k][n]
        if len(buffer) < self.distance_avg_size:
            return False
        prev_x = 0
        deltas = []
        for i in range(len(buffer) - self.distance_avg_size, len(buffer)):
            x, _, _, _ = buffer[i]
            if i == 0:
                prev_x = x
            else:
                delta = x - prev_x
                deltas.append(delta)
                prev_x = x
        del deltas[0]
        avg = sum(deltas) / self.distance_avg_size - 1
        self.delta_buffer[k][n].append(avg >= 0)

        print("delta_buffer", self.delta_buffer)
        increasing = True
        opposite_count = 0
        increasing_list = []
        for i, d in enumerate(self.delta_buffer[k][n]):
            if i == 0:
                increasing = d
            elif d != increasing:
                opposite_count += 1
            increasing_list.append(d)

        print("frame:", frame_idx, "cam:", num_cam, "list:", increasing_list, "deltas:", deltas, "avg:", avg)
        return opposite_count > len(self.delta_buffer[k][n]) // 2

    def _visualize(self, num_cam):
        for i in range(num_cam):
            fig = plt.figure(figsize=(8, 8))
            plt.gca().invert_yaxis()
            frame = 0
            for idx, (x, y) in enumerate(zip(self.xpos_meas_save[i], self.ypos_meas_save[i])):
                plt.scatter(x, y, s=300, c="r", marker='*')
                if idx == 0:
                    plt.text(x + 0.1, y, "{}".format(frame + 70), fontsize=10)
                    plt.legend(["Position: Measurements"])
                else:
                    plt.text(x + 0.1, y, "{}".format(frame + 70), fontsize=10)
                frame += 1
            frame = 0
            for idx, (x, y) in enumerate(zip(self.xpos_esti_save[i], self.ypos_esti_save[i])):
                plt.scatter(x, y, s=120, c="b", marker='o')
                if idx == 0:
                    plt.text(x + 0.1, y, "{}".format(frame + 70), fontsize=10)
                    # plt.text(x + 0.1, y, "{}".format(frame), fontsize=10, label="Position: Estimation (KF)")
                    plt.legend(["Position: Estimation (KF)"])
                else:
                    plt.text(x + 0.1, y, "{}".format(frame + 70), fontsize=10)
                frame += 1

            plt.legend(loc='lower right')
            plt.title('Position: Meas. v.s. Esti. (KF), Camera[%d]' % i)
            plt.xlabel('X-pos. [m]')
            plt.ylabel('Y-pos. [m]')
            plt.xlim((-10, 3840))
            plt.ylim((2160, -10))
            fig.canvas.draw()
            plt.savefig("plt_{}.jpg".format(i), dpi=300)
            plt.pause(0.05)
            plt.clf()


    def _best_score(self):

        return self.bbox_buffers[6][-(self.buffer_size // 2)]

    def run_focal(self, optional_box=None, debug=None, visdom_info=None, save_results=False, external_tracker=None):
        config = ConfigParser('./config.json')
        write_bbox = config['write_bbox']
        write_conf = config['write_conf']
        write_time = config['write_time']
        is_record = config['is_record']
        video_name = config['video_name']
        start_frame_num = config['start_frame_num']
        last_frame_num = config['last_frame_num']
        start_focal_num = config['start_focal_num']
        last_focal_num = config['last_focal_num']
        plenoptic_video_name = config['plenoptic_video_name']
        plenoptic_save_path = config['plenoptic_save_path']
        timestamp = datetime.now().strftime(r'%m%d_%H%M%S')
        save_path = Path(config['save_path']) / timestamp
        save_path.mkdir(parents=True, exist_ok=True)
        params = self.get_parameters()

        external_tracker.external_init(plenoptic_save_path, timestamp, plenoptic_video_name, start_frame_num, last_frame_num,
                                       start_focal_num, last_focal_num, self.buffer_size)

        debug_ = debug
        if debug is None:
            debug_ = getattr(params, 'debug', 0)
        params.debug = debug_

        params.tracker_name = self.name
        params.param_name = self.parameter_name

        output_boxes = []
        ff_list = []

        cv2.namedWindow(video_name, cv2.WND_PROP_FULLSCREEN)
        focaldataloader = Load2DFolder(video_name, 'focal', frame_range=(start_frame_num, last_frame_num),
                                          focal_range=None)

        def _build_init_info(box):
            return {'init_bbox': box}

        frame_idx = start_frame_num
        bbox_color = [[0, 0, 255], [0, 255, 0], [255, 0, 0], [0, 0, 0], [255, 255, 255], [255, 255, 0], [0, 255, 255], [128, 128, 128], [128, 0, 0], [128, 128, 0], [0, 128, 0], [128, 0, 128], [0, 128, 128], [0, 0, 128], [255, 0, 255], [138, 43, 226]]


        is_first_frame = True
        print("Please type the number of trackers: ")
        tracker_num = int(sys.stdin.readline())
        tracker: list[list[any]] = []
        best_idx = [0 for _ in range(tracker_num)]
        d_index = [5 for _ in range(tracker_num)]
        baseline = False
        self.new_bbox = [None for _ in range(tracker_num)]
        self.plenoptic_running = [PlenOpticState.NOT_RUNNING for _ in range(tracker_num)]

        num_cam = 0
        bbox_avg_for_plenoptic = None

        for frame, focal_image, focals, all_frame in focaldataloader:

            bbox_list = [frame_idx, *[0 for _ in range(tracker_num * 4)]]
            conf_list = [frame_idx, *[0 for _ in range(tracker_num)]]
            time_list = [frame_idx, *[0 for _ in range(tracker_num)]]

            plenoptic_done_items = external_tracker.external_call(frame_idx)
            if plenoptic_done_items is not None:
                for k, v in plenoptic_done_items.items():
                    if v["done"]:
                        self.plenoptic_running[k] = PlenOpticState.NOT_RUNNING
                        self.bbox_buffers[k][-1] = deque([])
                        for i in range(len(all_frame)):
                            self.bbox_buffers[k][i] = deque([])
                            self.score_buffers[k][i] = deque([])
                            self.delta_buffer[k][i] = deque([])

                            params = self.get_parameters()
                            debug_ = debug
                            if debug is None:
                                debug_ = getattr(params, 'debug', 0)
                            params.debug = debug_
                            params.tracker_name = self.name
                            params.param_name = self.parameter_name
                            tracker[k][i] = self.create_tracker(params)
                            tracker[k][i].initialize(frame[0], _build_init_info(v["best_state"]))

                    bbox_list[k * 4 + 1:k * 4 + 5] = v["best_state"]
                    conf_list[k + 1] = round(v["max_score"], 5)
                    time_list[k + 1] = round(v["tracking_time"], 2)
                    self.draw_bboxes(frame[0], k, v["best_state"], bbox_color[k])

            print("focals_len", len(focals))
            if frame_idx > start_frame_num:
                is_first_frame = False

            if is_first_frame:
                num_cam = len(all_frame)
                for k in range(tracker_num):
                    _trackers = []
                    _bboxes = []
                    _scores = []
                    _deltas = []
                    for i in range(num_cam):
                        _tracker = self.create_tracker(params)
                        _tracker = deepcopy(_tracker)
                        _trackers.append(_tracker)
                        _bboxes.append(deque([]))
                        _scores.append(deque([]))
                        _deltas.append(deque([]))
                    _bboxes.append(deque([]))
                    tracker.append(_trackers)
                    self.bbox_buffers.append(_bboxes)
                    self.score_buffers.append(_scores)
                    self.delta_buffer.append(_deltas)
                for k in range(tracker_num):
                    print('framelen', len(frame))
                    cv2.putText(frame[0], 'Select target ROI and press ENTER', (20, 30), cv2.FONT_HERSHEY_COMPLEX_SMALL,
                                1.5, (0, 0, 0), 1)

                    x, y, w, h = cv2.selectROI(video_name, frame[0], fromCenter=False)
                    init_state = [x, y, w, h]
                    for i in range(num_cam):
                        tracker[k][i].initialize(frame[0], _build_init_info(init_state))
                        output_boxes.append(init_state)

            print(f'{frame_idx}th frame start!')

            for k in range(tracker_num):
                if self.plenoptic_running[k] == PlenOpticState.RUNNING:
                    continue

                do_plenoptic = False
                frame_idx_for_plenoptic = 0
                if not is_first_frame:
                    for i in range(num_cam):
                        tracker[k][i].first_track = False

                tracking_time_start = time.time()
                max_score = 0
                best_state = [0, 0, 0, 0]

                range_focals = all_frame
                print("range_focals:", range_focals)

                bbox_avg = [0, 0, 0, 0]
                for i, f in enumerate(range_focals):
                    print("range_focals:", range_focals)
                    if i == len(range_focals) - 1:
                        tracker[k][i].last_sequence = True
                    a = f.split("\\")
                    path = a[-1]
                    _, frame_, camera = path.split("_")
                    ff = camera.split(".")[0]

                    img = cv2.imread(f)
                    print("f:", f)

                    out = tracker[k][i].track_multi_state(img, f'{frame_idx}_{ff}')
                    state = [int(s) for s in out[
                        'target_bbox']]
                    score = float(out['conf_score'])
                    print(f'[{k + 1}th] focal {i} score: {score}')
                    self.draw_bboxes(frame[0], int(ff), state, bbox_color[int(ff)])

                    x, y, w, h = state
                    bbox_avg[0] += x
                    bbox_avg[1] += y
                    bbox_avg[2] += w
                    bbox_avg[3] += h

                    self.update_display = True
                    self.distance_threshold = 20000
                    self._save_buffer(k, state, score, i)
                    if not do_plenoptic:
                        do_plenoptic = self._distance(k, i, frame_idx, state, len(all_frame))
                    if do_plenoptic:
                        frame_idx_for_plenoptic = frame_idx
                        self.plenoptic_running[k] = PlenOpticState.NEED_START

                    if max_score < score:
                        print('best_score_camera', ff)
                        ff_list.append(ff)
                        max_score = score
                        output_boxes.append(state)
                        best_idx[k] = ff
                        best_state = state

                b = len(output_boxes) + 1
                a = frame[0]
                print(f'--[{k + 1}th] Best focal {best_idx[k]} score: {max_score}--')

                cam_len = len(range_focals)
                for i in range(4):
                    bbox_avg[i] /= cam_len
                    bbox_avg[i] = int(bbox_avg[i])
                x, y, w, h = bbox_avg
                cv2.rectangle(frame[0], (x, y), (x + w, y + h), (0, 0, 255), 15)
                self._save_buffer(k, bbox_avg, 0, -1)

                if max_score > 0.5:
                    d_index[k] = 3
                else:
                    d_index[k] = 5

                conf = 0.0
                for i in self.score_buffers[k]:
                    conf += i[-1]
                conf /= len(self.score_buffers[k])

                bbox_list[k * 4 + 1:k * 4 + 5] = best_state
                conf_list[k + 1] = round(conf, 5)
                time_list[k + 1] = round(round(time.time() - tracking_time_start, 2), 2)

                print("do_lentoic", do_plenoptic)

                if self.plenoptic_running[k] == PlenOpticState.NEED_START:
                    self.plenoptic_done = True

                    external_tracker.external_init_call(k, frame_idx, self.bbox_buffers[k][-1], self.score_buffers[k])
                    self.plenoptic_running[k] = PlenOpticState.RUNNING

            if is_record:
                cv2.imwrite(f'{save_path}/{frame_idx:03d}.png', frame[0])

            if write_bbox:
                csv_write(save_path, bbox_list, 'bbox')
            if write_conf:
                csv_write(save_path, conf_list, 'conf')
            if write_time:
                csv_write(save_path, time_list, 'time')
            frame_idx += 1

            cv2.imshow(video_name, frame[0])
            if cv2.waitKey(1) & 0xFF == ord('q'):
                exit()

        cv2.destroyAllWindows()


    def run_2d(self, optional_box=None, debug=None, visdom_info=None, save_results=False):
        """Run the tracker with the vieofile.
        args:
            debug: Debug level.
        """
        from imageDataloader import Load2DFolder
        from config_parser import ConfigParser
        import torch
        from datetime import datetime
        import cv2

        config = ConfigParser('./config.json')
        write_bbox = config['write_bbox']
        write_time = config['write_time']
        # write_gt = config['write_gt']
        is_record = config['is_record']
        video_name = config['video_name']
        video_type = config['video_type']
        img2d_ref = config['image2d_ref']
        start_frame_num = config['start_frame_num']
        last_frame_num = config['last_frame_num']
        start_focal_num = config['start_focal_num']
        last_focal_num = config['last_focal_num']
        timestamp = datetime.now().strftime(r'%m%d_%H%M%S')
        save_path = Path(config['save_path']) / timestamp
        device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

        params = self.get_parameters()

        debug_ = debug
        if debug is None:
            debug_ = getattr(params, 'debug', 0)
        params.debug = debug_

        params.tracker_name = self.name
        params.param_name = self.parameter_name

        multiobj_mode = getattr(params, 'multiobj_mode', getattr(self.tracker_class, 'multiobj_mode', 'default'))

        if multiobj_mode == 'default':
            tracker = self.create_tracker(params)

        output_boxes = []

        cv2.namedWindow(video_name, cv2.WND_PROP_FULLSCREEN)
        imagedataloader = Load2DFolder(video_name, frame_range=(start_frame_num, last_frame_num))

        def _build_init_info(box):
            return {'init_bbox': box}

        k = 0
        frame_idx = start_frame_num
        max_score = 0
        best_state = None
        best_idx = [0]
        d_index = [5]
        is_first_frame = True

        for frame in imagedataloader:
            if is_first_frame:
                x, y, w, h = cv2.selectROI(video_name, frame, fromCenter=False)
                init_state = [x, y, w, h]
                tracker.initialize(frame, _build_init_info(init_state))
                is_first_frame = False
                continue

            print(f'{frame_idx}th frame start!')
            tracking_time_start = time.time()
            # Draw box

            out = tracker.track_one_state(frame)
            state = [int(s) for s in out['target_bbox']]
            score = round(out['conf_score'], 2)
            print(f'score: {score}')

            cv2.rectangle(frame, (state[0], state[1]), (state[2] + state[0], state[3] + state[1]),
                         (0, 255, 0), 5)
            cv2.imwrite(f'{save_path}/{frame_idx:03d}.png', frame)
            if write_bbox:
                csv_write(save_path, state, 'bbox')

            if write_time:
                csv_write(save_path, [round(time.time()-tracking_time_start, 2)], 'time')
            frame_idx += 1
            cv2.imshow(video_name, frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                exit()


    def get_parameters(self):
        """Get parameters."""
        param_module = importlib.import_module('lib.test.parameter.{}'.format(self.name))
        params = param_module.parameters(self.parameter_name)
        return params

    def _read_image(self, image_file: str):
        if isinstance(image_file, str):
            im = cv.imread(image_file)
            return cv.cvtColor(im, cv.COLOR_BGR2RGB)
        elif isinstance(image_file, list) and len(image_file) == 2:
            return decode_img(image_file[0], image_file[1])
        else:
            raise ValueError("type of image_file should be str or list")
