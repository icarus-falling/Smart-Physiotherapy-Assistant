import cv2
import mediapipe as mp
import numpy as np
import math
from datetime import datetime
import statistics
# import pyttsx3  # --- REMOVED ---
import threading
import queue
import time
import os      # --- NEW: For creating folders/paths ---
import csv     # --- NEW: For saving CSV log ---
import tempfile # --- NEW: For gTTS ---
import requests
from gtts import gTTS # --- NEW: For gTTS ---
from playsound import playsound # --- NEW: For gTTS ---


class VibrationClient:
    """Non-blocking vibration sender.

    Uses a background thread and a requests.Session with keep-alive to
    send JSON POSTs to the ESP32 endpoint `/vibrate`. Queueing ensures
    the main (video) thread never blocks on network I/O.
    """
    def __init__(self, host='http://esp32-haptic.local', max_queue=50):
        self.base = host.rstrip('/')
        self.url = f"{self.base}/vibrate"
        self.session = requests.Session()
        self.q = queue.Queue()
        self.max_queue = max_queue
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def vibrate(self, side='BOTH', duration_ms=200, intensity=255):
        """Queue a vibration command. Non-blocking.

        side: 'LEFT', 'RIGHT', or 'BOTH'
        duration_ms: integer ms
        intensity: 0-255
        """
        if not self._running:
            return

        # Prevent unbounded queue growth: drop oldest if necessary
        try:
            while self.q.qsize() >= self.max_queue:
                try:
                    self.q.get_nowait()
                except Exception:
                    break
        except Exception:
            pass

        payload = {
            "action": "on",
            "side": side if side else "BOTH",
            "duration_ms": int(duration_ms),
            "intensity": int(intensity)
        }
        try:
            self.q.put_nowait(payload)
        except Exception:
            # fallback: try blocking put (shouldn't happen often)
            try:
                self.q.put(payload, timeout=0.01)
            except Exception:
                pass

    def stop(self):
        self._running = False
        try:
            # wake the worker
            self.q.put(None)
        except Exception:
            pass
        try:
            self._thread.join(timeout=1.0)
        except Exception:
            pass

    def _worker(self):
        while self._running:
            try:
                item = self.q.get(timeout=0.05)
                if item is None:
                    break
                try:
                    # short timeout so a failed send doesn't hang
                    self.session.post(self.url, json=item, timeout=0.5)
                except Exception as e:
                    # keep going; we don't want to block the main process
                    print(f"Vibration send error: {e}")
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Vibration worker error: {e}")
                continue



class SmartPhysioDemoAssistant:
    def __init__(self, exercise, session_mode): # --- MODIFIED: Added session_mode ---
        self.exercise = exercise.lower()
        self.mppose = mp.solutions.pose
        self.pose = self.mppose.Pose(static_image_mode=False, model_complexity=1,
                                     enable_segmentation=False, min_detection_confidence=0.5,
                                     min_tracking_confidence=0.5)
        self.mpdrawing = mp.solutions.drawing_utils
        self.current_ex = self.exercise
        self.FPS = 30

        # --- MODIFICATION START: Replaced pyttsx3 with gTTS/playsound system ---
        self.audio_queue = queue.Queue()
        self.audio_cache = {}  # Cache generated audio files
        self.temp_dir = tempfile.gettempdir()
        
        # Pre-generate common audio files
        print("Initializing audio system...")
        self._pregenerarate_audio()
        
        self.audio_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self.audio_thread.start()
        # --- NEW: Non-blocking vibration client (talks to ESP32 web endpoint) ---
        vib_host = os.environ.get('VIBRATION_HOST', 'http://esp32-haptic.local')
        try:
            self.vib_client = VibrationClient(vib_host)
            print(f"Vibration client initialized -> {vib_host}")
        except Exception as e:
            print(f"Warning: could not initialize VibrationClient: {e}")
        # --- MODIFICATION END ---

        # --- NEW: Session control variables ---
        self.session_mode = session_mode # "solo" or "assisted"
        self.session_active = False # Master switch for evaluation
        self.start_time = time.time() # For countdown timers
        self.last_status_message = ""
        self.last_key_press_time = 0

        # --- MODIFIED: Performance Metric Collectors ---
        self.frame_latencies = []
        self.total_frames_captured = 0    # Counter for all frames read from camera
        self.total_frames_processed = 0   # Counter for frames where pose was detected

        # --- MODIFIED: Updated Perfect Thresholds (Single Value) ---
        self.squat_perfect_angle = 90    # Perfect if angle <= 90
        self.abd_perfect_angle = 150   # Perfect if angle >= 150
        self.eflex_perfect_angle = 40    # Perfect if angle <= 40
        self.hflex_perfect_angle = 100   # Perfect if angle <= 100
        self.wext_perfect_angle = 120   # Perfect if angle <= 120

        # --- MODIFIED: Updated Correct/Rest Angles based on user request ---
        self.down_knee_angle = 110   # "Correct" Squat is <= 110
        self.up_knee_angle = 160     # Rest angle for Squat
        self.abd_down_angle = 30     # Rest angle for Abduction
        self.abd_up_angle = 90     # "Correct" Abduction is >= 90
        self.abd_max_angle = 170     # Upper limit for Abduction correctness (can be adjusted if needed)
        self.eflex_straight_angle = 160 # Rest angle for Elbow Flexion
        self.eflex_bent_angle = 70     # "Correct" Elbow is <= 70
        self.hflex_straight_angle = 165 # Rest angle for Hip Flexion
        self.hflex_bent_angle = 120    # "Correct" Hip is <= 120
        self.wext_straight_angle = 165 # Rest angle for Wrist Extension
        self.wext_bent_angle = 135   # "Correct" Wrist is <= 135

        self.exercises = {
            "squat": self._get_state(),
            "abduction": self._get_state(),
            "elbow": self._get_state(),
            "hipflex": self._get_state(),
            "wristext": self._get_state()
        }

        if self.session_mode == "assisted":
            self.last_status_message = "SESSION PAUSED"
        else:
             self.last_status_message = "" # Solo mode starts with countdown

    def _get_state(self):
        """ Returns a clean state dictionary for an exercise """
        return {
            "repcount": 0, "phase": "none", "ready": False,
            "start_frames_needed": 12, "start_frames_counter": 0,
            "rep_scores": [], "rep_start_frame": 0,
            "current_rep_perfect_frames": 0,
            "current_rep_standard_frames": 0,
            "error_persistence_counter": 0, 
            "audio_lock_perfect": False, # Kept for "Good" -> "Perfect" logic
            "audio_lock_correct": False, # Kept for "Good" -> "Perfect" logic
            "frame_counter": 0,
            "session_data": [], "feedback": "", "tracked_side": "NONE",
            "current_rep_best_angle": 180 if self.current_ex != "abduction" else 0,
            "rest_persistence_counter": 0,
            "in_incorrect_attempt": False,
            "last_stopped_frame": 0,
            "session_ended_for_ex": False,
            # --- NEW Flags for one-time audio cues ---
            "played_get_ready": False,
            "played_session_start": False,
        }

    def _get_default_data(self):
        """ Returns a blank data dictionary for a paused state """
        state = self.exercises[self.current_ex]
        return {"angle": 0, "repcount": state.get('repcount', 0), "phase": "NONE", "correct_form": False,
                "form_status": "NONE", "last_score": 0,
                "avg_score": np.mean(state['rep_scores']) if state['rep_scores'] else 0,
                "feedback": ""}

    # --- MODIFICATION START: New gTTS methods ---
    def _pregenerarate_audio(self):
        """Pre-generate audio files for common phrases"""
        # --- MODIFICATION: Shortened audio cues ---
        common_phrases = [
            "Good", "Perfect",    # <-- CHANGED
            "Try again",          # <-- CHANGED
            "You stopped", "Session ended", 
            "SESSION START",
            "SESSION PAUSED",
            "GET READY",
            "SESSION RESUMED"
        ] + [str(i) for i in range(1, 51)]  # Numbers 1-50
        
        for phrase in common_phrases:
            try:
                filename = os.path.join(self.temp_dir, f"audio_{hash(phrase)}.mp3")
                if not os.path.exists(filename):
                    tts = gTTS(text=phrase, lang='en', slow=False)
                    tts.save(filename)
                self.audio_cache[phrase] = filename
            except Exception as e:
                print(f"Failed to generate audio for '{phrase}': {e}")
        
        print("Audio system ready!")

    def _audio_worker(self):
        """Worker thread that handles all audio playback"""
        while True:
            try:
                # --- MODIFICATION: Reduced sleep/wait time for faster response ---
                message = self.audio_queue.get(timeout=0.01) # <-- CHANGED from 0.1
                if message is None:
                    break
                
                print(f"🔊 SPEAKING: {message}")
                
                if message in self.audio_cache:
                    audio_file = self.audio_cache[message]
                else:
                    audio_file = os.path.join(self.temp_dir, f"audio_{hash(message)}.mp3")
                    tts = gTTS(text=message, lang='en', slow=False)
                    tts.save(audio_file)
                    self.audio_cache[message] = audio_file
                
                playsound(audio_file)
                
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Audio error: {e}")

    def play_audio(self, message):
        """Queue audio message for playback"""
        try:
            # Clear queue if backing up
            while self.audio_queue.qsize() > 2:
                try:
                    self.audio_queue.get_nowait()
                except:
                    break
            
            print(f"AUDIO CUE: {message}")
            self.audio_queue.put(message)
        except Exception as e:
            print(f"Error queuing audio: {e}")
    # --- MODIFICATION END ---


    def calc_angle(self, a, b, c):
        if not all([a, b, c]): return 180
        if a.visibility < 0.5 or b.visibility < 0.5 or c.visibility < 0.5:
             return 180

        pa = np.array([a.x, a.y]); pb = np.array([b.x, b.y]); pc = np.array([c.x, c.y])
        vba = pa - pb; vbc = pc - pb

        norm_vba = np.linalg.norm(vba)
        norm_vbc = np.linalg.norm(vbc)
        if norm_vba == 0 or norm_vbc == 0:
            return 180

        cosine = np.dot(vba, vbc) / (norm_vba * norm_vbc)
        cosine = np.clip(cosine, -1.0, 1.0)
        return np.degrees(np.arccos(cosine))


    def get_bilateral_angles(self, lm, ex_type):
        angles = {}
        lmk = lambda i: lm[i] if i < len(lm) else None

        if ex_type == "squat":
            angles['right'] = self.calc_angle(lmk(24), lmk(26), lmk(28))
            angles['left'] = self.calc_angle(lmk(23), lmk(25), lmk(27))
        elif ex_type == "abduction":
            angles['right'] = self.calc_angle(lmk(24), lmk(12), lmk(14))
            angles['left'] = self.calc_angle(lmk(23), lmk(11), lmk(13))
        elif ex_type == "elbow":
            angles['right'] = self.calc_angle(lmk(12), lmk(14), lmk(16))
            angles['left'] = self.calc_angle(lmk(11), lmk(13), lmk(15))
        elif ex_type == "hipflex":
            angles['right'] = self.calc_angle(lmk(12), lmk(24), lmk(26))
            angles['left'] = self.calc_angle(lmk(11), lmk(23), lmk(25))
        elif ex_type == "wristext":
            angles['right'] = self.calc_angle(lmk(14), lmk(16), lmk(20))
            angles['left'] = self.calc_angle(lmk(13), lmk(15), lmk(19))

        return angles

    def log_failed_rep(self, state, ex_type, threshold_min):
        best_angle = state["current_rep_best_angle"]

        if (ex_type != "abduction" and best_angle == 180) or \
           (ex_type == "abduction" and best_angle == 0):
            return

        deviation = 0
        if ex_type == "abduction":
            deviation = threshold_min - best_angle
        else:
            deviation = best_angle - threshold_min

        score = 0
        if deviation < 0:
            score = np.random.randint(60, 71)
        elif deviation < 15: # Near Miss
            score = np.random.randint(60, 71)
        elif deviation < 30: # Clear Error
            score = np.random.randint(40, 60)
        else: # Significant Error
            score = np.random.randint(20, 40)

        state["rep_scores"].append(score)

    def calculate_rep_score(self, status_type, perfect_quality_ratio=0):
        if status_type == "SUCCESS":
            if perfect_quality_ratio > 0.8: return np.random.randint(95, 101)
            elif perfect_quality_ratio > 0.5: return np.random.randint(85, 95)
            else: return np.random.randint(75, 85)
        return 0

    def check_form_correct(self, angle, ex):
        if ex == "squat": return angle <= self.down_knee_angle
        elif ex == "abduction": return self.abd_up_angle <= angle <= self.abd_max_angle
        elif ex == "elbow": return angle <= self.eflex_bent_angle
        elif ex == "hipflex": return angle <= self.hflex_bent_angle
        elif ex == "wristext": return angle <= self.wext_bent_angle
        return False

    def check_perfect_form(self, angle, ex):
        if ex == "squat": return angle <= self.squat_perfect_angle
        elif ex == "abduction": return angle >= self.abd_perfect_angle
        elif ex == "elbow": return angle <= self.eflex_perfect_angle
        elif ex == "hipflex": return angle <= self.hflex_perfect_angle
        elif ex == "wristext": return angle <= self.wext_perfect_angle
        return False

    def analyze_form(self, lm, ex):

        state = self.exercises[ex]
        state["frame_counter"] += 1
        current_frame = state["frame_counter"]

        angles = self.get_bilateral_angles(lm, ex)
        angle_L, angle_R = angles.get('left', 180), angles.get('right', 180)

        angle, tracked_side = (0, "NONE")
        if ex == "squat" or ex == "elbow" or ex == "hipflex" or ex == "wristext":
            angle, tracked_side = min(angle_L, angle_R), "LEFT" if angle_L < angle_R else "RIGHT"
        elif ex == "abduction":
            angle, tracked_side = max(angle_L, angle_R), "LEFT" if angle_L > angle_R else "RIGHT"
        state["tracked_side"] = tracked_side

        default_best_angle = 180
        if ex == "squat": rest_phase_angle, active_phase_name, rest_phase_name, active_threshold = self.up_knee_angle, "down", "up", self.down_knee_angle
        elif ex == "abduction": rest_phase_angle, active_phase_name, rest_phase_name, active_threshold = self.abd_down_angle, "up", "down", self.abd_up_angle; default_best_angle = 0
        elif ex == "elbow": rest_phase_angle, active_phase_name, rest_phase_name, active_threshold = self.eflex_straight_angle, "bent", "straight", self.eflex_bent_angle
        elif ex == "hipflex": rest_phase_angle, active_phase_name, rest_phase_name, active_threshold = self.hflex_straight_angle, "flexed", "straight", self.hflex_bent_angle
        elif ex == "wristext": rest_phase_angle, active_phase_name, rest_phase_name, active_threshold = self.wext_straight_angle, "up", "down", self.wext_bent_angle

        is_correct = self.check_form_correct(angle, ex)
        in_perfect = self.check_perfect_form(angle, ex)

        if not state["ready"]:
            in_start_pos = False
            if ex == "squat" or ex == "hipflex":
                in_start_pos = angle_L > rest_phase_angle and angle_R > rest_phase_angle
            elif ex == "abduction":
                 in_start_pos = angle_L < rest_phase_angle and angle_R < rest_phase_angle
            else:
                in_start_pos = angle > rest_phase_angle

            if in_start_pos:
                state["start_frames_counter"] += 1
                if state["start_frames_counter"] >= state["start_frames_needed"]:
                    state["ready"], state["phase"], state["rep_start_frame"] = True, rest_phase_name, current_frame
                    state["current_rep_best_angle"] = default_best_angle
            else: state["start_frames_counter"] = 0
            return {"angle": angle, "repcount": 0, "phase": "NONE", "correct_form": False, "form_status": "NONE", "last_score": 0, "avg_score": 0, "feedback": "Get into starting position."}

        prev_phase = state["phase"]

        rep_completed = False
        if ex == "squat":
            if angle_L < self.down_knee_angle or angle_R < self.down_knee_angle: state["phase"] = "down"
            elif angle_L > self.up_knee_angle and angle_R > self.up_knee_angle and prev_phase == "down": rep_completed, state["phase"] = True, "up"
        elif ex == "abduction":
            if angle > self.abd_up_angle: state["phase"] = "up"
            elif angle < self.abd_down_angle and prev_phase == "up": rep_completed, state["phase"] = True, "down"
        elif ex == "elbow":
            if angle < self.eflex_bent_angle: state["phase"] = "bent"
            elif angle > self.eflex_straight_angle and prev_phase == "bent": rep_completed, state["phase"] = True, "straight"
        elif ex == "hipflex":
            if angle < self.hflex_bent_angle: state["phase"] = "flexed"
            elif angle_L > self.hflex_straight_angle and angle_R > self.hflex_straight_angle and prev_phase == "flexed":
                rep_completed, state["phase"] = True, "straight"
        elif ex == "wristext":
            if angle < self.wext_bent_angle: state["phase"] = "up"
            elif angle > self.wext_straight_angle and prev_phase == "up": rep_completed, state["phase"] = True, "down"

        is_active_phase = state["phase"] == active_phase_name
        if is_active_phase:
            if in_perfect:
                state["current_rep_perfect_frames"] += 1
            elif is_correct:
                state["current_rep_standard_frames"] += 1

        if state["phase"] != prev_phase and state["phase"] == active_phase_name:
             state["audio_lock_perfect"], state["audio_lock_correct"] = False, False

        if rep_completed:
            state["repcount"] += 1
            total_active_correct_frames = state["current_rep_standard_frames"] + state["current_rep_perfect_frames"]
            perfect_quality_ratio = 0
            if total_active_correct_frames > 0:
                 perfect_quality_ratio = state["current_rep_perfect_frames"] / total_active_correct_frames

            score = self.calculate_rep_score(status_type="SUCCESS", perfect_quality_ratio=perfect_quality_ratio)
            state["rep_scores"].append(score)

            self.play_audio(str(state["repcount"])) 

            state["rep_start_frame"] = current_frame
            state["current_rep_perfect_frames"] = 0
            state["current_rep_standard_frames"] = 0
            state["current_rep_best_angle"] = default_best_angle
            state["in_incorrect_attempt"] = False

        is_in_rest_phase = False
        if ex == "squat": is_in_rest_phase = angle_L > self.up_knee_angle and angle_R > self.up_knee_angle
        elif ex == "abduction": is_in_rest_phase = angle < self.abd_down_angle
        elif ex == "elbow": is_in_rest_phase = angle > self.eflex_straight_angle
        elif ex == "hipflex": is_in_rest_phase = angle_L > self.hflex_straight_angle and angle_R > self.hflex_straight_angle
        elif ex == "wristext": is_in_rest_phase = angle > self.wext_straight_angle

        is_stopped = False
        is_holding_incorrect = not is_correct and not is_in_rest_phase

        if is_in_rest_phase:
            state["rest_persistence_counter"] += 1
            state["error_persistence_counter"] = 0

            if state["in_incorrect_attempt"]:
                self.log_failed_rep(state, ex, active_threshold)
                self.play_audio("Try again") # <-- CHANGED
                state["in_incorrect_attempt"] = False
                state["current_rep_best_angle"] = default_best_angle

            if self.session_mode == "solo" and state["rest_persistence_counter"] >= (15 * self.FPS):
                if not state["session_ended_for_ex"]:
                    self.session_active = False
                    state["session_ended_for_ex"] = True
                    self.last_status_message = "SESSION ENDED"
                    self.play_audio("Session ended")
                    state["rest_persistence_counter"] = 0

            elif state["rest_persistence_counter"] >= (5 * self.FPS):
                is_stopped = True 

                if (current_frame - state["last_stopped_frame"]) >= 150:
                    self.play_audio("You stopped") 
                    state["last_stopped_frame"] = current_frame

        elif is_holding_incorrect:
            state["in_incorrect_attempt"] = True
            state["rest_persistence_counter"] = 0
            state["last_stopped_frame"] = 0

            if ex == "abduction":
                state["current_rep_best_angle"] = max(state["current_rep_best_angle"], angle)
            else:
                state["current_rep_best_angle"] = min(state["current_rep_best_angle"], angle)

            state["error_persistence_counter"] += 1
            if state["error_persistence_counter"] == 5:
                # trigger a short haptic pulse on the tracked side (non-blocking)
                try:
                    side = tracked_side if tracked_side in ("LEFT", "RIGHT") else "BOTH"
                    # short buzz to alert user; asynchronous via VibrationClient
                    if hasattr(self, 'vib_client') and self.vib_client:
                        self.vib_client.vibrate(side=side, duration_ms=250, intensity=255)
                    else:
                        print(f"HAPTIC (no client): Vibrate {side} - Incorrect form!")
                except Exception as _e:
                    print(f"HAPTIC trigger failed: {_e}")

        elif is_correct: 
            state["rest_persistence_counter"] = 0
            state["error_persistence_counter"] = 0
            state["last_stopped_frame"] = 0

            if state["in_incorrect_attempt"]:
                 state["in_incorrect_attempt"] = False
                 state["current_rep_best_angle"] = default_best_angle
        else:
            state["rest_persistence_counter"] = 0


        current_status = "NONE"
        if is_stopped:
            current_status = "STOPPED"
        elif in_perfect:
            current_status = "PERFECT"
        elif is_correct:
            current_status = "CORRECT"
        elif is_holding_incorrect:
            current_status = "INCORRECT"

        # --- MODIFICATION: Shortened audio cues ---
        if is_active_phase:
            if in_perfect:
                if not state["audio_lock_perfect"]: 
                    self.play_audio("Perfect") # <-- CHANGED
                    state["audio_lock_perfect"], state["audio_lock_correct"] = True, True
            elif is_correct: 
                if not state["audio_lock_correct"]: 
                    self.play_audio("Good") # <-- CHANGED
                    state["audio_lock_correct"] = True

        avg_score = np.mean(state["rep_scores"]) if state["rep_scores"] else 0
        return {"angle": angle, "repcount": state["repcount"], "phase": state["phase"], "correct_form": is_correct,
                "form_status": current_status,
                "last_score": state["rep_scores"][-1] if state["rep_scores"] else 0,
                "avg_score": avg_score, "feedback": ""}

    def draw_landmarks(self, frame, pose_landmarks, form_status, ex):
        involved = {"squat": [23, 25, 27, 24, 26, 28],
                    "abduction": [11, 13, 23, 12, 14, 24],
                    "elbow": [11, 13, 15, 12, 14, 16],
                    "hipflex": [11, 23, 25, 12, 24, 26],
                    "wristext": [13, 15, 19, 14, 16, 20]
                   }.get(ex, [])
        NEUTRAL_COLOR, CORRECT_COLOR, INCORRECT_COLOR = (255, 0, 0), (0, 255, 0), (0, 0, 255)

        color_map = {"PERFECT": CORRECT_COLOR, "CORRECT": CORRECT_COLOR, "INCORRECT": INCORRECT_COLOR, "NONE": NEUTRAL_COLOR, "STOPPED": NEUTRAL_COLOR}
        color = color_map.get(form_status, NEUTRAL_COLOR)
        h, w, _ = frame.shape

        if not pose_landmarks or not pose_landmarks.landmark:
             return frame

        landmarks_list = pose_landmarks.landmark
        num_landmarks = len(landmarks_list)

        for idx, lm in enumerate(landmarks_list):
            if lm.visibility < 0.5: continue
            cx, cy = int(lm.x * w), int(lm.y * h)
            point_color = color if idx in involved else NEUTRAL_COLOR
            radius = 7 if idx in involved else 5
            cv2.circle(frame, (cx, cy), radius, point_color, -1)

        for conn in self.mppose.POSE_CONNECTIONS:
             idx1, idx2 = conn
             if idx1 >= num_landmarks or idx2 >= num_landmarks: continue

             pt1, pt2 = landmarks_list[idx1], landmarks_list[idx2]
             if pt1.visibility < 0.5 or pt2.visibility < 0.5: continue

             x1, y1 = int(pt1.x * w), int(pt1.y * h)
             x2, y2 = int(pt2.x * w), int(pt2.y * h)
             conn_color = color if (idx1 in involved and idx2 in involved) else NEUTRAL_COLOR
             cv2.line(frame, (x1, y1), (x2, y2), conn_color, 2)

        return frame


    def draw_feedback(self, frame, data):
        h, w = frame.shape[:2]
        title = {"squat": "Squats", "abduction": "Shoulder Abduction", "elbow": "Elbow Flexion",
                 "hipflex": "Hip Flexion", "wristext": "Wrist Extension"
                }.get(self.current_ex, "Exercise")

        cv2.rectangle(frame, (10, 10), (int(w - 10), 170), (40, 40, 40), -1)

        cv2.putText(frame, title, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2)
        cv2.putText(frame, f"Reps: {data.get('repcount', 0)}", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

        current_phase = data.get('phase', 'none')
        phase_text = current_phase.upper() if current_phase != "none" else "NONE"
        phase_color = (128, 128, 128)

        if (self.current_ex == "abduction" and current_phase == "up") or \
           (self.current_ex == "squat" and current_phase == "down") or \
           (self.current_ex == "wristext" and current_phase == "up") or \
           (current_phase in ["bent", "flexed"]):
            phase_color = (0, 165, 255)
        elif (self.current_ex == "abduction" and current_phase == "down") or \
             (self.current_ex == "squat" and current_phase == "up") or \
             (self.current_ex == "wristext" and current_phase == "down") or \
             (current_phase == "straight"):
            phase_color = (0, 255, 0)

        if current_phase == 'bent': phase_text = 'BENT'
        elif current_phase == 'flexed': phase_text = 'FLEXED'
        elif current_phase == 'straight': phase_text = 'STRAIGHT'

        cv2.putText(frame, f"Phase: {phase_text}", (200, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.75, phase_color, 2)

        form_status = data.get('form_status', 'NONE')
        status_color = (255, 0, 0)
        status_text = form_status 

        if form_status in ['PERFECT', 'CORRECT']:
            status_color = (0, 255, 0)
        elif form_status == 'INCORRECT':
             status_color = (0, 0, 255)
        elif form_status == 'STOPPED':
             status_color = (0, 165, 255)
             status_text = "YOU STOPPED"

        cv2.putText(frame, f"Form: {status_text}", (20, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)

        last_score = data.get('last_score', 0)
        avg_score = data.get('avg_score', 0)

        cv2.putText(frame, f"Score: {last_score:.1f}", (300, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(frame, f"Avg: {avg_score:.1f}", (510, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 150, 255), 2)

        help_text = "Press 1 (Start) / 0 (Pause)" if self.session_mode == "assisted" else "s/a/e/h/w to switch, q to quit."
        cv2.putText(frame, help_text, (20, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.53, (255, 255, 255), 1)
        if self.session_mode == "assisted":
            cv2.putText(frame, "s/a/e/h/w to switch, q to quit.", (20, 155), cv2.FONT_HERSHEY_SIMPLEX, 0.53, (255, 255, 255), 1)


        return frame

    def draw_session_status(self, frame, message):
        """ Draws large status text in the center of the frame. """
        h, w = frame.shape[:2]
        font_scale = 1.5
        thickness = 3

        (text_width, text_height), baseline = cv2.getTextSize(message, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        text_x = (w - text_width) // 2
        text_y = (h + text_height) // 2

        cv2.rectangle(frame, (text_x - 20, text_y - text_height - 20), (text_x + text_width + 20, text_y + baseline + 10), (0, 0, 0), -1)
        cv2.putText(frame, message, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 255, 255), thickness) # Yellow
        return frame


    def run(self):
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
             print("Error: Could not open video capture device.")
             return

        cap.set(3, 1280)
        cap.set(4, 720)
        cap.set(5, self.FPS)

        window_name = "SmartPhysio"
        cv2.namedWindow(window_name)

        while cap.isOpened():
            frame_start_time = time.time()

            ret, frame = cap.read()
            
            self.total_frames_captured += 1
            
            if not ret:
                print("Error: Failed to capture frame.")
                break

            frame = cv2.flip(frame, 1)
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            result = self.pose.process(rgb_frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"): break

            if self.session_mode == "assisted" and (time.time() - self.last_key_press_time > 0.5):
                if key == ord('1'): # Start/Resume
                    self.session_active = True
                    self.last_status_message = "SESSION RESUMED"
                    self.play_audio("SESSION RESUMED")
                    self.last_key_press_time = time.time()
                elif key == ord('0'): # Pause/End
                    self.session_active = False
                    self.last_status_message = "SESSION PAUSED"
                    self.play_audio("SESSION PAUSED")
                    self.last_key_press_time = time.time()

            new_ex = None
            if key == ord("s"): new_ex = "squat"
            elif key == ord("a"): new_ex = "abduction"
            elif key == ord("e"): new_ex = "elbow"
            elif key == ord("h"): new_ex = "hipflex"
            elif key == ord("w"): new_ex = "wristext"

            if new_ex and new_ex != self.current_ex:
                print(f"\nSwitching to {new_ex}...")
                self.current_ex = new_ex
                self.exercises[self.current_ex] = self._get_state() # Resets all flags
                self.start_time = time.time()
                self.session_active = False
                if self.session_mode == "assisted":
                    self.last_status_message = "SESSION PAUSED"
                else:
                    self.last_status_message = ""

            state = self.exercises[self.current_ex]
            session_status_message = ""

            if self.session_mode == "solo":
                if state["session_ended_for_ex"]:
                    session_status_message = "SESSION ENDED"
                    self.session_active = False
                elif not self.session_active:
                    elapsed = time.time() - self.start_time
                    countdown = 15 - int(elapsed)
                    if countdown > 0:
                        session_status_message = f"GET READY: {countdown}"
                        if not state["played_get_ready"]:
                            self.play_audio("GET READY")
                            state["played_get_ready"] = True
                    else:
                        self.session_active = True
                        session_status_message = "SESSION START"
                        if not state["played_session_start"]:
                            self.play_audio("SESSION START")
                            state["played_session_start"] = True
                        
                        if time.time() - self.start_time > 16:
                             self.last_status_message = ""
                             session_status_message = ""
                        else:
                             self.last_status_message = "SESSION START"

            else: # Assisted Mode
                session_status_message = self.last_status_message
                if (time.time() - self.last_key_press_time > 1):
                    if self.session_active:
                         self.last_status_message = ""
                         session_status_message = ""
                    else:
                         self.last_status_message = "SESSION PAUSED"
                         session_status_message = "SESSION PAUSED"

            if result.pose_landmarks and result.pose_landmarks.landmark:
                
                self.total_frames_processed += 1

                if self.session_active:
                    data = self.analyze_form(result.pose_landmarks.landmark, self.current_ex)
                    frame = self.draw_landmarks(frame, result.pose_landmarks, data['form_status'], self.current_ex)
                    frame = self.draw_feedback(frame, data)

                    if not self.session_active and self.session_mode == "solo":
                        session_status_message = self.last_status_message

                else: 
                    data = self._get_default_data()
                    frame = self.draw_landmarks(frame, result.pose_landmarks, "NONE", self.current_ex)
                    frame = self.draw_feedback(frame, data)

            else: 
                data = self._get_default_data()
                frame = self.draw_feedback(frame, data)
                cv2.putText(frame, "No pose detected - body must be visible.", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

            if session_status_message:
                frame = self.draw_session_status(frame, session_status_message)

            cv2.imshow(window_name, frame)

            frame_end_time = time.time()
            latency = frame_end_time - frame_start_time
            self.frame_latencies.append(latency)


        cap.release()
        cv2.destroyAllWindows()
        self.audio_queue.put(None)
        self.audio_thread.join()
        # Stop vibration worker cleanly
        try:
            if hasattr(self, 'vib_client') and self.vib_client:
                self.vib_client.stop()
        except Exception as e:
            print(f"Error stopping vibration client: {e}")

        if not self.frame_latencies:
            avg_latency = 0
        else:
            avg_latency = np.mean(self.frame_latencies)
        
        if self.total_frames_captured == 0:
            frame_processing_efficiency = 0.0
        else:
            frame_processing_efficiency = (self.total_frames_processed / self.total_frames_captured) * 100
        
        session_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        folder_name = "session_metrics"
        file_name = os.path.join(folder_name, "performance_log.csv")
        
        try:
            if not os.path.exists(folder_name):
                os.makedirs(folder_name)
        except OSError as e:
            print(f"Error creating directory {folder_name}: {e}")

        headers = [
            "Timestamp", "SessionMode", "Exercise", "TotalReps", "AverageScore",
            "RepScores", "AvgLatency_sec", "FrameProcessingEfficiency_Percent"
        ]
        
        file_exists = os.path.isfile(file_name)
        
        try:
            with open(file_name, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                
                if not file_exists:
                    writer.writerow(headers)
                    file_exists = True
            
                for ex in ["squat", "abduction", "elbow", "hipflex", "wristext"]:
                    st = self.exercises[ex]
                    
                    if st['repcount'] > 0 or st['rep_scores']:
                        print(f"\n--- {ex.title()} SESSION SUMMARY ---")
                        print(f"Total reps: {st['repcount']}")
                        
                        formatted_scores = [f"{score:.0f}" for score in st['rep_scores']]
                        scores_str = f"[{', '.join(formatted_scores)}]"
                        print(f"Rep Scores: {scores_str}")
                        
                        avg_score = np.mean(st['rep_scores']) if st['rep_scores'] else 0
                        print(f"Average Score: {avg_score:.1f}")
                        print("------------------------\n")

                        session_data_row = [
                            session_timestamp,
                            self.session_mode,
                            ex,
                            st['repcount'],
                            f"{avg_score:.2f}",
                            scores_str,
                            f"{avg_latency:.2f}",
                            f"{frame_processing_efficiency:.2f}"
                        ]
                        
                        writer.writerow(session_data_row)
            
            print(f"\nSuccessfully appended metrics to {file_name}")
            
        except IOError as e:
            print(f"Error writing to CSV file {file_name}: {e}")

        print("\n--- SYSTEM PERFORMANCE METRICS ---")
        print(f"Feedback Latency (avg): {avg_latency:.2f} sec")
        print(f"Frame Processing Efficiency: {frame_processing_efficiency:.2f} %")
        

if __name__ == "__main__":
    mode = ""
    while mode not in ["solo", "assisted"]:
        mode = input("Enter session mode ('solo' or 'assisted'): ").strip().lower()
        if mode not in ["solo", "assisted"]:
            print("Invalid mode. Please type 'solo' or 'assisted'.")

    exercise = input("Enter exercise ('squat', 'abduction', 'elbow', 'hipflex', or 'wristext'): ").strip().lower()
    if exercise not in ["squat", "abduction", "elbow", "hipflex", "wristext"]:
        print(f"Invalid exercise '{exercise}'. Defaulting to 'squat'.")
        exercise = "squat"

    assistant = SmartPhysioDemoAssistant(exercise, mode)
    assistant.run()