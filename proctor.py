#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
import tkinter as tk

import cv2
import customtkinter as ctk
import numpy as np
import psutil
from PIL import Image

import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision
from ultralytics import YOLO

try:
    import keyboard  # на Linux/macOS нужен root
except Exception:  # noqa: BLE001
    keyboard = None
try:
    import pyttsx3
except Exception:  # noqa: BLE001
    pyttsx3 = None
try:
    import winsound
except ImportError:
    winsound = None

FACE_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
                  "face_landmarker/float16/latest/face_landmarker.task")
FACE_MODEL_PATH = Path("models/face_landmarker.task")
COCO_PERSON, COCO_PHONE = 0, 67
BROWSER_BAR = 34  # высота верхней полосы окна Chrome (px при 100% масштабе); подстройте, если видно край/срезан верх
BOTTOM_OVERSCAN = 20  # на сколько px (при 100% масштабе) окно теста выступает за нижний край экрана, чтобы не было полоски

# --------------------------------------------------------------------------- #
# Win32 (только Windows): управление окнами
# --------------------------------------------------------------------------- #
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # реальные пиксели
    except Exception:  # noqa: BLE001
        pass
    _u32 = ctypes.windll.user32
    _u32.GetForegroundWindow.restype = wintypes.HWND
    _u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.GetWindowLongW.restype = ctypes.c_long
    _u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _u32.SetWindowLongW.restype = ctypes.c_long
    _u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _u32.SetWindowPos.restype = wintypes.BOOL
    _u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _u32.IsWindowVisible.argtypes = [wintypes.HWND]
    _u32.IsIconic.argtypes = [wintypes.HWND]
    _u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _u32.GetWindow.restype = wintypes.HWND
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]


def work_area() -> tuple[int, int, int, int] | None:
    """Рабочая область экрана (без панели задач) в реальных пикселях; только Windows."""
    if sys.platform != "win32":
        return None
    try:
        r = wintypes.RECT()
        if _u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
            return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------- #
# Палитра
# --------------------------------------------------------------------------- #
BG, CARD, CARD2, TRACK = "#0e1120", "#171b2f", "#1f2440", "#2a3050"
TEXT, MUTED, ACCENT = "#e8ebf8", "#8a90b0", "#6c8cff"
GREEN, YELLOW, ORANGE, RED = (61, 220, 151), (255, 209, 102), (255, 159, 67), (255, 84, 112)  # RGB для risk_color
GREEN_HEX, RED_HEX = "#3ddc97", "#ff5470"  # строки для виджетов customtkinter


def risk_color(p: float) -> str:
    stops = [(0.0, GREEN), (0.35, YELLOW), (0.60, ORANGE), (1.0, RED)]
    p = min(max(p, 0.0), 1.0)
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if p <= b:
            t = (p - a) / (b - a)
            return "#%02x%02x%02x" % tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))
    return "#%02x%02x%02x" % RED


# ==== i18n ==================================================================
LANG = "ru"
LANG_LABELS = {"kk": "Қазақша", "ru": "Русский", "en": "English"}
_IDX = {"ru": 0, "kk": 1, "en": 2}

TR = {
    # --- статусы движка ---
    "st.loading": ("Загрузка моделей…", "Модельдер жүктелуде…", "Loading models…"),
    "st.err": ("Ошибка загрузки моделей: {e}", "Модельдерді жүктеу қатесі: {e}", "Model loading error: {e}"),
    "st.cam": ("Камера недоступна", "Камера қолжетімсіз", "Camera unavailable"),
    "st.calib": ("Калибровка… смотрите на экран ({n}/{total})",
                 "Калибрлеу… экранға қараңыз ({n}/{total})",
                 "Calibrating… look at the screen ({n}/{total})"),
    "st.ok": ("Всё в порядке", "Бәрі жақсы", "All good"),
    # --- уровни риска ---
    "lvl.low": ("Низкий риск", "Төмен тәуекел", "Low risk"),
    "lvl.mid": ("Умеренный риск", "Орташа тәуекел", "Moderate risk"),
    "lvl.high": ("Высокий риск", "Жоғары тәуекел", "High risk"),
    "lvl.crit": ("Критический риск", "Сыни тәуекел", "Critical risk"),
    # --- названия факторов ---
    "sig.phone": ("Смартфон", "Смартфон", "Smartphone"),
    "sig.raised": ("Телефон у экрана", "Телефон экран жанында", "Phone near screen"),
    "sig.cover": ("Камера перекрыта", "Камера жабылған", "Camera blocked"),
    "sig.down": ("Взгляд вниз", "Төмен қарау", "Looking down"),
    "sig.side": ("Взгляд в стороны и вверх", "Бүйірге және жоғары қарау", "Looking aside / up"),
    "sig.head": ("Поворот головы", "Бастың бұрылуы", "Head turn"),
    "sig.noface": ("Нет в кадре", "Кадрда жоқ", "Not in frame"),
    "sig.multi": ("Второй человек", "Екінші адам", "Second person"),
    "sig.window": ("Посторонние окна", "Бөтен терезелер", "Other windows"),
    "sig.apps": ("Запрещённые программы", "Тыйым салынған бағдарламалар", "Forbidden programs"),
    "sig.keys": ("Запрещённые клавиши", "Тыйым салынған пернелер", "Forbidden keys"),
    # --- сообщения журнала ---
    "msg.phone": ("Обнаружен смартфон", "Смартфон анықталды", "Smartphone detected"),
    "msg.raised": ("Телефон на уровне лица — возможная съёмка экрана",
                   "Телефон бет деңгейінде — экранды суретке түсіру мүмкін",
                   "Phone at face level — possible screen capture"),
    "msg.cover": ("Перекрыт обзор веб-камеры", "Веб-камераның көрінісі жабылған", "Webcam view is blocked"),
    "msg.down": ("Длительный взгляд вниз (телефон/шпаргалка)",
                 "Ұзақ төмен қарау (телефон/шпаргалка)",
                 "Prolonged looking down (phone/notes)"),
    "msg.side": ("Взгляд в сторону или вверх (второй монитор/шпаргалка)",
                 "Бүйірге немесе жоғары қарау (екінші монитор/шпаргалка)",
                 "Looking aside or up (second monitor/notes)"),
    "msg.head": ("Голова повёрнута или наклонена от экрана",
                 "Бас экраннан бұрылған немесе еңкейген",
                 "Head turned or tilted away from the screen"),
    "msg.noface": ("Человек отсутствует в кадре", "Адам кадрда жоқ", "Person is not in frame"),
    "msg.multi": ("В кадре второй человек", "Кадрда екінші адам бар", "A second person is in frame"),
    "msg.window": ("Переключение на постороннее окно", "Бөтен терезеге ауысу", "Switched to another window"),
    "msg.apps": ("Запрещённое приложение", "Тыйым салынған қолданба", "Forbidden application"),
    "msg.keys": ("Попытка запрещённого сочетания клавиш",
                 "Тыйым салынған перне тіркесімін басу әрекеті",
                 "Attempt to use a forbidden key combination"),
    "win.closed": ("Окно теста закрыто", "Тест терезесі жабылды", "Test window closed"),
    "app.found": ("Запрещённое приложение: {name}{closed}",
                  "Тыйым салынған қолданба: {name}{closed}",
                  "Forbidden application: {name}{closed}"),
    "app.closed": (" (закрыто)", " (жабылды)", " (closed)"),
    "app.initial": ("Закрыта при запуске: {name} (без штрафа)",
                    "Іске қосу кезінде жабылды: {name} (айыппұлсыз)",
                    "Closed at startup: {name} (no penalty)"),
    "key.try": ("Попытка: {hk}", "Әрекет: {hk}", "Attempt: {hk}"),
    "guard.nomod": ("Модуль keyboard недоступен — блокировка клавиш отключена",
                    "keyboard модулі қолжетімсіз — пернелерді бұғаттау өшірулі",
                    "keyboard module unavailable — key blocking disabled"),
    "guard.failed": ("Не удалось заблокировать {n} комбинаций",
                     "{n} тіркесімді бұғаттау мүмкін болмады",
                     "Could not block {n} combinations"),
    "guard.err": ("Блокировка клавиш не запущена: {e}. Нужны права администратора/root",
                  "Пернелерді бұғаттау іске қосылмады: {e}. Әкімші/root құқығы қажет",
                  "Key blocking did not start: {e}. Administrator/root rights required"),
    "guard.novoice": ("Модуль pyttsx3 недоступен — только звуковой сигнал",
                      "pyttsx3 модулі қолжетімсіз — тек дыбыстық сигнал",
                      "pyttsx3 module unavailable — beep only"),
    "voice.warn": ("Внимание, обнаружено нарушение! Вернитесь к тесту.",
                   "Назар аударыңыз, бұзушылық анықталды! Тестке оралыңыз.",
                   "Attention, a violation has been detected! Please return to the test."),
    "fin.log": ("Итоговая вероятность: {p}%", "Қорытынды ықтималдық: {p}%", "Final probability: {p}%"),
    # --- стартовый экран ---
    "start.subtitle": ("Локальный прокторинг · Computer Vision + Security",
                       "Жергілікті прокторинг · Computer Vision + Security",
                       "Local proctoring · Computer Vision + Security"),
    "start.name": ("Ваше имя", "Атыңыз", "Your name"),
    "start.name_ph": ("Фамилия Имя", "Тегі Аты", "Full name"),
    "start.url": ("Ссылка на тест (Google Forms и т.п.)", "Тест сілтемесі (Google Forms т.б.)",
                  "Test link (Google Forms, etc.)"),
    "start.sw_keys": ("Блокировать горячие клавиши", "Жедел пернелерді бұғаттау", "Block hotkeys"),
    "start.sw_kill": ("Закрывать запрещённые программы", "Тыйым салынған бағдарламаларды жабу",
                      "Close forbidden programs"),
    "start.sw_voice": ("Голосовые предупреждения", "Дауыстық ескертулер", "Voice warnings"),
    "start.info": (
        "• Тест откроется слева, панель прокторинга — справа\n• Переключение на другие окна и программы фиксируется\n"
        "• Камера: смартфон, взгляд, второй человек\n• Первые секунды — калибровка: смотрите на экран",
        "• Тест сол жақта, прокторинг тақтасы оң жақта ашылады\n• Басқа терезелер мен бағдарламаларға ауысу тіркеледі\n"
        "• Камера: смартфон, көз қарасы, екінші адам\n• Алғашқы секундтар — калибрлеу: экранға қараңыз",
        "• The test opens on the left, the proctoring panel on the right\n• Switching to other windows and programs is recorded\n"
        "• Camera: smartphone, gaze, second person\n• First seconds — calibration: look at the screen"),
    "start.btn": ("Далее", "Әрі қарай", "Next"),
    "start.go": ("Начать проверку", "Тексеруді бастау", "Start proctoring"),
    "start.loading": ("Запуск…", "Іске қосылуда…", "Starting…"),
    "default.student": ("Студент", "Студент", "Student"),
    "warn.title": ("Браузер", "Браузер", "Browser"),
    "warn.browser": ("Chrome/Edge не найден — работаем без контроля окон.",
                     "Chrome/Edge табылмады — терезелерді бақылаусыз жұмыс істейміз.",
                     "Chrome/Edge not found — running without window control."),
    # --- правила ---
    "rules.title": ("Правила прохождения теста", "Тест тапсыру ережелері", "Test rules"),
    "rules.1": ("Уберите со стола и из зоны камеры все посторонние предметы: телефон, книги, конспекты, наушники, смарт-часы.",
                "Үстелден және камера көрінетін аймақтан барлық бөгде заттарды алып тастаңыз: телефон, кітаптар, конспектілер, құлаққап, ақылды сағат.",
                "Remove all unrelated items from your desk and the camera view: phone, books, notes, headphones, smartwatch."),
    "rules.2": ("Вы должны быть в комнате один. Во время теста других людей в комнате быть не должно.",
                "Бөлмеде жалғыз болыңыз. Тест кезінде бөлмеде басқа адамдар болмауы тиіс.",
                "Be alone in the room. No other people may be present during the test."),
    "rules.3": ("Сядьте напротив камеры: лицо и плечи должны быть хорошо видны. Обеспечьте достаточное освещение.",
                "Камераға қарама-қарсы отырыңыз: бетіңіз бен иығыңыз жақсы көрінуі керек. Жарық жеткілікті болсын.",
                "Sit facing the camera so your face and shoulders are clearly visible. Make sure the lighting is good."),
    "rules.4": ("Смотрите на экран. Не отворачивайтесь и без необходимости не смотрите по сторонам, вверх или вниз.",
                "Экранға қараңыз. Бұрылмаңыз және қажет болмаса жан-жаққа, жоғары немесе төмен қарамаңыз.",
                "Look at the screen. Do not turn away or look around, up or down without need."),
    "rules.5": ("Не закрывайте веб-камеру рукой или шторкой и не загораживайте её.",
                "Веб-камераны қолмен немесе шторкамен жаппаңыз және жасырмаңыз.",
                "Do not cover or block the webcam with your hand or a shutter."),
    "rules.6": ("Закройте все остальные программы и вкладки. Переключение окон и горячие клавиши (Alt+Tab, Win, Ctrl+C/V, PrtScn) запрещены и фиксируются.",
                "Барлық басқа бағдарламалар мен қойындыларды жабыңыз. Терезелерді ауыстыру және жедел пернелер (Alt+Tab, Win, Ctrl+C/V, PrtScn) тыйым салынған және тіркеледі.",
                "Close all other programs and tabs. Switching windows and hotkeys (Alt+Tab, Win, Ctrl+C/V, PrtScn) are forbidden and recorded."),
    "rules.7": ("Первые секунды — калибровка: сядьте прямо и смотрите в центр экрана.",
                "Алғашқы секундтарда калибрлеу жүреді: тік отырып, экранның ортасына қараңыз.",
                "The first seconds are calibration: sit straight and look at the center of the screen."),
    "rules.8": ("Каждое нарушение фиксируется и озвучивается. Итог — вероятностная оценка; окончательное решение принимает преподаватель.",
                "Әрбір бұзушылық тіркеліп, дауыспен ескертіледі. Нәтиже — ықтималдық бағасы; түпкілікті шешімді оқытушы қабылдайды.",
                "Every violation is recorded and announced. The result is a probabilistic estimate; the final decision is made by the instructor."),
    "rules.agree": ("Я прочитал(а) правила и согласен(на)", "Ережелерді оқыдым және келісемін",
                    "I have read the rules and agree"),
    "rules.back": ("Назад", "Артқа", "Back"),
    # --- рабочая панель ---
    "mon.title": ("ВЕРОЯТНОСТЬ СПИСЫВАНИЯ", "КӨШІРУ ЫҚТИМАЛДЫҒЫ", "CHEATING PROBABILITY"),
    "tab.factors": ("Факторы", "Факторлар", "Factors"),
    "tab.log": ("Журнал", "Журнал", "Log"),
    "mon.finish": ("Завершить проверку", "Тексеруді аяқтау", "Finish proctoring"),
    "confirm.title": ("Завершение", "Аяқтау", "Finish"),
    "confirm.text": ("Завершить проверку?\nУбедитесь, что тест отправлен.",
                     "Тексеруді аяқтайсыз ба?\nТест жіберілгенін тексеріңіз.",
                     "Finish proctoring?\nMake sure the test has been submitted."),
    # --- отчёт ---
    "rep.title": ("Отчёт о проверке", "Тексеру есебі", "Proctoring report"),
    "rep.final": ("ИТОГОВАЯ ВЕРОЯТНОСТЬ СПИСЫВАНИЯ", "КӨШІРУДІҢ ҚОРЫТЫНДЫ ЫҚТИМАЛДЫҒЫ",
                  "FINAL CHEATING PROBABILITY"),
    "rep.peak": ("Пик", "Шыңы", "Peak"),
    "rep.avg": ("Среднее", "Орташа", "Average"),
    "rep.events": ("Событий", "Оқиғалар", "Events"),
    "rep.clean": ("Нарушений не зафиксировано ✓", "Бұзушылықтар тіркелмеді ✓", "No violations recorded ✓"),
    "rep.row": ("{sec:.0f} с · {ev} соб.", "{sec:.0f} с · {ev} оқиға", "{sec:.0f} s · {ev} ev."),
    "rep.note": ("Оценка вероятностная; окончательное решение принимает преподаватель.",
                 "Баға ықтималдық сипатында; түпкілікті шешімді оқытушы қабылдайды.",
                 "The estimate is probabilistic; the final decision is made by the instructor."),
    "rep.open": ("Открыть папку отчёта", "Есеп папкасын ашу", "Open report folder"),
    "rep.close": ("Закрыть", "Жабу", "Close"),
}


def tr(key: str, **kw) -> str:
    text = TR[key][_IDX[LANG]]
    return text.format(**kw) if kw else text
# ==== /i18n ==================================================================


def level_text(p: float) -> str:
    return tr("lvl.low" if p < 0.25 else "lvl.mid" if p < 0.5 else "lvl.high" if p < 0.75 else "lvl.crit")


# --------------------------------------------------------------------------- #
# Модель риска
# --------------------------------------------------------------------------- #
# КАК РЕГУЛИРОВАТЬ ПРОЦЕНТЫ. Итог = 1 - Π(1 - вклад_i),  вклад = вес * уровень.
# Поля строки:
#   key / label - ключ и подпись     weight - ПОТОЛОК: макс. % от одного сигнала (0.40 = 40%)
#   hold   - сколько секунд условие должно держаться, чтобы засчитаться
#   rise   - прирост накопителя A в секунду при нарушении   decay - спад A в секунду без нарушения
#   tau    - масштаб: уровень = 1 - exp(-A/tau); меньше tau = быстрее к потолку
#   bump   - разовая добавка к A за каждый эпизод/попытку   cooldown - пауза между записями в журнал (с)
# ВАЖНО: down / side / head считаются иначе (см. RateSignal): уровень = доля времени «не на экране»
# за скользящее окно 5 минут относительно нормы. Для них rise/decay/tau/bump не используются -
# настройка идёт через RATE (пороги f0/f1 по доле времени, e0/e1 по числу эпизодов в минуту).
SIGNALS = [
    ("phone",  "Смартфон",              0.92, 1.0, 1.0, 0.020, 2.0, 0.0, 6),   # нужно 3-5 с, чтобы вырасти до высоких %
    ("raised", "Телефон у экрана",      0.97, 0.6, 1.5, 0.020, 1.5, 0.5, 6),   # возможная съёмка монитора
    ("cover",  "Камера перекрыта",      0.85, 1.0, 1.5, 0.050, 3.0, 1.5, 8),
    ("down",   "Взгляд вниз",           0.75, 0.5, 0.0, 0.0,   1.0, 0.0, 8),   # RateSignal (строгий режим)
    ("side",   "Взгляд в стороны/вверх", 0.50, 0.8, 0.0, 0.0,  1.0, 0.0, 8),   # RateSignal
    ("head",   "Поворот головы",        0.35, 1.0, 0.0, 0.0,   1.0, 0.0, 8),   # RateSignal
    ("noface", "Нет в кадре",           0.50, 2.0, 1.0, 0.200, 15., 0.0, 10),
    ("multi",  "Второй человек",        0.75, 1.0, 1.0, 0.050, 3.0, 1.0, 10),
    ("window", "Посторонние окна",      0.65, 1.0, 1.0, 0.050, 4.0, 1.5, 6),
    ("apps",   "Запрещённые программы", 0.70, 0.0, 0.0, 0.020, 3.0, 3.0, 0),
    ("keys",   "Запрещённые клавиши",   0.40, 0.0, 0.0, 0.050, 5.0, 1.5, 0),
]
# f0 - «норма» (доля времени вне экрана, ниже которой риск 0), f1 - доля, при которой уровень = 100%,
# e0/e1 - эпизодов в минуту: ниже e0 = 0, выше e1 = 100%
RATE = {"down": (0.10, 0.40, 3.0, 8.0), "side": (0.15, 0.55, 4.0, 12.0), "head": (0.20, 0.60, 3.0, 10.0)}
# СТРОГИЙ режим (взгляд вниз): «норма» не подстраивается под студента и не растёт с усталостью, а
# дополнительно считается НАКОПЛЕННАЯ доля взгляда вниз за ВЕСЬ тест (c0 - допустимо, c1 - 100%).
# Накопленный уровень почти не забывается (спад ~100% за час), поэтому привычка «чуть-чуть поглядывать
# вниз» постоянно набирает штраф, а не растворяется в 5-минутном окне.
STRICT = {"down"}
CUM = {"down": (0.04, 0.22)}
ATTN = ("down", "side", "head")  # группа «внимание»: вклады не складываются полностью
STRONG = {"phone", "raised", "multi", "apps", "cover"}
VOICE_CODES = {"phone", "raised", "cover", "multi", "head", "side", "down", "noface", "window"}
GRACE = 0.4  # допустимый «провал» детекции, чтобы мерцание не сбрасывало эпизод


class _Messages:
    def __getitem__(self, code: str) -> str:
        return tr("msg." + code)


MESSAGES = _Messages()


def clamp01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


@dataclass
class Signal:
    key: str
    label: str
    weight: float
    hold: float
    rise: float
    decay: float
    tau: float
    bump: float
    cooldown: float
    A: float = 0.0
    t: float = field(default_factory=time.time)
    since: float | None = None
    off_since: float | None = None
    stable: bool = False
    last_fire: float = -1e9
    active_time: float = 0.0

    def reset(self, now: float):
        self.A, self.t, self.since, self.off_since = 0.0, now, None, None
        self.stable, self.last_fire, self.active_time = False, -1e9, 0.0

    def advance(self, now: float):
        dt = max(0.0, now - self.t)
        self.t = now
        if self.stable:
            self.A += self.rise * dt
            self.active_time += dt
        else:
            self.A -= self.decay * dt
        self.A = min(max(self.A, 0.0), self.tau * 4)

    def on_episode(self, now: float):
        pass

    def set(self, active: bool, now: float) -> bool:
        """True - начался новый эпизод (пора писать в журнал)."""
        self.advance(now)
        if active:
            self.off_since = None
            if self.since is None:
                self.since = now
            was = self.stable
            self.stable = now - self.since >= self.hold
            if self.stable and not was:
                self.on_episode(now)
                if now - self.last_fire >= self.cooldown:
                    self.last_fire = now
                    self.A = min(self.A + self.bump, self.tau * 4)
                    return True
        elif self.since is not None:
            if self.off_since is None:
                self.off_since = now
            if now - self.off_since >= GRACE:
                self.since = self.off_since = None
                self.stable = False
        return False

    def add(self, now: float, n: float | None = None):
        self.advance(now)
        self.A = min(self.A + (self.bump if n is None else n), self.tau * 4)

    def level(self, now: float) -> float:
        self.advance(now)
        return 1 - math.exp(-self.A / self.tau)


@dataclass
class RateSignal(Signal):
    """Для взгляда/головы. Уровень зависит от ДОЛИ времени «вне экрана» за последние 5 минут
    (а не от суммы за весь тест), поэтому он не растёт бесконечно на часовом экзамене.
    Порог «нормы» немного поднимается с усталостью и с личным поведением студента."""
    f0: float = 0.2
    f1: float = 0.6
    e0: float = 4.0
    e1: float = 12.0
    window: float = 300.0
    t0: float = field(default_factory=time.time)
    base: float = 0.0
    _lt: float = field(default_factory=time.time)
    buckets: deque = field(default_factory=deque)
    episodes: deque = field(default_factory=deque)
    strict: bool = False
    c0: float = 0.04
    c1: float = 0.22
    cum: float = 0.0

    def reset(self, now: float):
        super().reset(now)
        self.t0 = self._lt = now
        self.base = 0.0
        self.cum = 0.0
        self.buckets.clear()
        self.episodes.clear()

    def advance(self, now: float):
        dt = max(0.0, now - self.t)
        self.t = now
        if self.stable and dt > 0:
            self.active_time += dt
            sec = int(now)
            if self.buckets and self.buckets[-1][0] == sec:
                self.buckets[-1][1] += dt
            else:
                self.buckets.append([sec, dt])

    def on_episode(self, now: float):
        self.episodes.append(now)

    def level(self, now: float) -> float:
        self.advance(now)
        W = self.window
        while self.buckets and self.buckets[0][0] < now - W:
            self.buckets.popleft()
        while self.episodes and self.episodes[0] < now - W:
            self.episodes.popleft()
        elapsed = now - self.t0
        span = max(min(W, elapsed), 60.0)  # первые минуты «разбавляем», чтобы не было ложных всплесков
        frac = sum(b[1] for b in self.buckets) / span
        dt, self._lt = max(0.0, now - self._lt), now
        if self.strict:  # строго: никакой подстройки «нормы» под студента и усталость
            low = self.f0
        else:
            if elapsed > 120:  # личная норма подстраивается медленно (~20 мин)
                self.base += (frac - self.base) * min(1.0, dt / 1200.0)
            fatigue = min(0.06, elapsed / 3600.0 * 0.06)  # час экзамена: норма «сдвигается» до +6 п.п.
            # итоговая «норма» не может уйти выше f0 + 12 п.п., иначе настоящее списывание станет «нормой»
            low = min(self.f0 + 0.12, max(self.f0 + fatigue, self.base + 0.05))
        lf = clamp01((frac - low) / max(1e-6, self.f1 - low))
        rate = len(self.episodes) / span * 60.0
        le = clamp01((rate - self.e0) / max(1e-6, self.e1 - self.e0))
        lv = max(lf, 0.8 * le)
        if self.strict:
            # накопленная доля за весь тест; сам уровень почти не забывается
            total = self.active_time / max(elapsed, 60.0)
            lc = clamp01((total - self.c0) / max(1e-6, self.c1 - self.c0))
            self.cum = max(lc, self.cum - dt / 3600.0)
            lv = max(lv, self.cum)
        return lv


class RiskModel:
    """Вероятность = 1 - Π(1 - вес_i * уровень_i)  (noisy-OR); взгляд/голова - группа с «мягким» суммированием."""
    def __init__(self):
        self.lock = threading.RLock()
        self.sig: dict[str, Signal] = {}
        for d in SIGNALS:
            if d[0] in RATE:
                f0, f1, e0, e1 = RATE[d[0]]
                c0, c1 = CUM.get(d[0], (0.04, 0.22))
                self.sig[d[0]] = RateSignal(*d, f0=f0, f1=f1, e0=e0, e1=e1,
                                            strict=d[0] in STRICT, c0=c0, c1=c1)
            else:
                self.sig[d[0]] = Signal(*d)
        for k, sg in self.sig.items():
            sg.label = tr("sig." + k)
        self.shown = self.peak = 0.0
        self.t_prev = self.t0 = time.time()
        self._last_hist = 0.0
        self._recent: deque = deque()
        self.hist: list[float] = []
        self.counts: dict[str, int] = defaultdict(int)
        self.armed = False  # пока идёт запуск и калибровка - риск не копится и равен 0

    def arm(self):
        """Полный сброс и старт подсчёта с 0% (после калибровки)."""
        with self.lock:
            now = time.time()
            for sg in self.sig.values():
                sg.reset(now)
            self.shown = self.peak = 0.0
            self.t_prev = self.t0 = now
            self._last_hist = 0.0
            self._recent.clear()
            self.hist.clear()
            self.counts.clear()
            self.armed = True

    def set(self, key: str, active: bool) -> bool:
        with self.lock:
            if not self.armed:
                return False
            return self.sig[key].set(active, time.time())

    def add(self, key: str, n: float | None = None):
        with self.lock:
            if self.armed:
                self.sig[key].add(time.time(), n)

    def snapshot(self):
        with self.lock:
            if not self.armed:
                zeros = {k: 0.0 for k in self.sig}
                return 0.0, dict(zeros), dict(zeros)
            now = time.time()
            lv = {k: s.level(now) for k, s in self.sig.items()}
            contrib = {k: self.sig[k].weight * lv[k] for k in lv}
            prod = 1.0
            for k, c in contrib.items():
                if k not in ATTN:
                    prod *= 1 - c
            att = sorted((contrib[k] for k in ATTN), reverse=True)
            prod *= 1 - min(0.85, att[0] + 0.35 * sum(att[1:]))
            target = 1 - prod
            dt = now - self.t_prev
            self.t_prev = now
            rate = 2.5 if target > self.shown else 0.35  # быстро растёт, медленно спадает
            self.shown += (target - self.shown) * min(1.0, rate * dt)
            # «устойчивый пик»: короткий всплеск (< 4 с) в пик не попадает
            self._recent.append((now, self.shown))
            while self._recent and self._recent[0][0] < now - 5.0:
                self._recent.popleft()
            if self._recent[-1][0] - self._recent[0][0] >= 4.0:
                self.peak = max(self.peak, min(v for _, v in self._recent))
            if now - self._last_hist >= 1.0:
                self.hist.append(self.shown)
                self._last_hist = now
            return self.shown, lv, contrib


# --------------------------------------------------------------------------- #
# Журнал
# --------------------------------------------------------------------------- #
class EventLog:
    def __init__(self):
        self.dir = Path("proctor_logs") / datetime.now().strftime("session_%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self.lock = threading.Lock()

    def write(self, code: str, text: str, frame=None) -> dict:
        ts = datetime.now()
        evt = {"time": ts.isoformat(timespec="seconds"), "code": code, "text": text}
        if frame is not None:
            name = f"{ts.strftime('%H%M%S')}_{code}.jpg"
            cv2.imwrite(str(self.dir / name), frame)
            evt["evidence"] = name
        with self.lock:
            self.file.write(json.dumps(evt, ensure_ascii=False) + "\n")
            self.file.flush()
        return evt

    def close(self):
        with self.lock:
            self.file.close()


# --------------------------------------------------------------------------- #
# Компьютерное зрение
# --------------------------------------------------------------------------- #
@dataclass
class Cfg:
    phone_conf: float = 0.45        # выше = меньше ложных «телефонов» в обычных предметах
    person_conf: float = 0.50
    phone_min_area: float = 0.004   # доля кадра; мельче - игнорируем
    phone_max_area: float = 0.55
    persist_window: float = 2.0     # телефон/второй человек должны быть видны ...
    persist_ratio: float = 0.60     # ... в >=60% кадров за последние 2 с, иначе не считается
    yolo_every: int = 2
    calib_frames: int = 45
    gaze_down_thr: float = 0.22     # пороги ПОСЛЕ вычитания личной «нормы» взгляда (вниз - чувствительнее)
    gaze_side_thr: float = 0.45
    gaze_up_thr: float = 0.30
    yaw_thr: float = 25.0
    pitch_thr: float = 20.0
    dark_abs: float = 22.0          # средняя яркость кадра (0-255) ниже = «чёрный экран»
    dark_rel: float = 0.35          # яркость < 35% от обычной = резкое затемнение
    flat_std: float = 8.0           # «плоская» картинка (палец на объективе)
    debug: bool = False


GK = ("down", "left", "right", "up")


class Persist:
    """Подтверждение по времени: событие считается, только если держится заметную часть окна."""
    def __init__(self, window: float, ratio: float):
        self.window, self.ratio = window, ratio
        self.h: deque = deque()

    def update(self, now: float, value: bool) -> bool:
        self.h.append((now, value))
        while self.h and self.h[0][0] < now - self.window:
            self.h.popleft()
        if self.h[-1][0] - self.h[0][0] < self.window * 0.6:
            return False
        return sum(v for _, v in self.h) / len(self.h) >= self.ratio

    def clear(self):
        self.h.clear()


def is_covered(luma: float, std: float, base: float | None, c: Cfg) -> bool:
    """Камера закрыта: кадр почти чёрный, резко потемнел или стал «плоским» относительно обычного."""
    if luma < c.dark_abs:
        return True
    if base is not None and base > 55:
        if luma < base * c.dark_rel:
            return True
        if std < c.flat_std and luma < base * 0.75:
            return True
    return False


def ensure_face_model() -> str:
    if not FACE_MODEL_PATH.exists():
        FACE_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(FACE_MODEL_URL, FACE_MODEL_PATH)
    return str(FACE_MODEL_PATH)


def head_angles(matrix) -> tuple[float, float]:
    R = np.array(matrix)[:3, :3].astype(float)
    R = R / (np.linalg.norm(R, axis=0, keepdims=True) + 1e-9)
    pitch = math.degrees(math.atan2(R[2, 1], R[2, 2]))
    yaw = math.degrees(math.atan2(-R[2, 0], math.hypot(R[2, 1], R[2, 2])))
    return yaw, pitch


def wrap(a: float) -> float:
    return (a + 180) % 360 - 180


class ProctorEngine(threading.Thread):
    def __init__(self, cfg: Cfg, log: EventLog, risk: RiskModel, cam: int,
                 yolo_path: str, device: str | None, alert=None):
        super().__init__(daemon=True)
        self.cfg, self.log, self.risk, self.cam = cfg, log, risk, cam
        self.yolo_path, self.device, self.alert = yolo_path, device, alert
        self.frames: queue.Queue = queue.Queue(maxsize=1)
        self.events: queue.Queue = queue.Queue()
        self.status, self.status_ok = tr("st.loading"), True
        self.stop_flag = threading.Event()
        self.last_det = {"phones": [], "persons": []}
        self.calib: list[tuple[float, float]] = []
        self.gcal: list[list[float]] = []
        self.base = (0.0, 0.0)
        self.gbase = {k: 0.0 for k in GK}
        self.base_luma: float | None = None
        self.p_phone = Persist(cfg.persist_window, cfg.persist_ratio)
        self.p_raised = Persist(cfg.persist_window, cfg.persist_ratio)
        self.p_multi = Persist(cfg.persist_window, cfg.persist_ratio)

    def emit(self, code: str, frame):
        self.events.put(self.log.write(code, MESSAGES[code], frame))
        if self.alert:
            self.alert(code)

    def detect_yolo(self, frame):
        h, w = frame.shape[:2]
        r = self.yolo.predict(frame, classes=[COCO_PERSON, COCO_PHONE],
                              conf=min(self.cfg.phone_conf, self.cfg.person_conf),
                              imgsz=640, device=self.device, verbose=False)[0]
        phones, persons = [], []
        for box, cls, conf in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.cls.cpu().numpy(),
                                  r.boxes.conf.cpu().numpy()):
            area = (box[2] - box[0]) * (box[3] - box[1]) / (w * h)
            if int(cls) == COCO_PHONE:
                if conf >= self.cfg.phone_conf and self.cfg.phone_min_area <= area <= self.cfg.phone_max_area:
                    phones.append((box, float(conf)))
            elif conf >= self.cfg.person_conf:
                persons.append((box, float(conf)))
        self.last_det = {"phones": phones, "persons": persons}

    @staticmethod
    def gaze_scores(blend) -> dict:
        b = {c.category_name: c.score for c in blend}
        return {
            "down": (b.get("eyeLookDownLeft", 0) + b.get("eyeLookDownRight", 0)) / 2,
            "up": (b.get("eyeLookUpLeft", 0) + b.get("eyeLookUpRight", 0)) / 2,
            "right": (b.get("eyeLookInLeft", 0) + b.get("eyeLookOutRight", 0)) / 2,
            "left": (b.get("eyeLookOutLeft", 0) + b.get("eyeLookInRight", 0)) / 2,
        }

    def run(self):
        try:
            self.yolo = YOLO(self.yolo_path)
            self.landmarker = vision.FaceLandmarker.create_from_options(
                vision.FaceLandmarkerOptions(
                    base_options=mp_python.BaseOptions(model_asset_path=ensure_face_model()),
                    running_mode=vision.RunningMode.VIDEO, num_faces=2,
                    output_face_blendshapes=True, output_facial_transformation_matrixes=True))
        except Exception as e:  # noqa: BLE001
            self.status, self.status_ok = tr("st.err", e=e), False
            return

        cap = cv2.VideoCapture(self.cam)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        if not cap.isOpened():
            self.status, self.status_ok = tr("st.cam"), False
            self.events.put(self.log.write("noface", tr("st.cam")))
            self.risk.arm()
            while not self.stop_flag.is_set():  # без камеры = «нет в кадре»
                self.risk.set("noface", True)
                time.sleep(0.5)
            return

        t0, n, c = time.time(), 0, self.cfg
        t_calib0 = t0
        while not self.stop_flag.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.02)
                continue
            n += 1
            now = time.time()
            h, w = frame.shape[:2]

            # --- перекрытие камеры ---
            small = cv2.cvtColor(cv2.resize(frame, (80, 60)), cv2.COLOR_BGR2GRAY)
            luma, std = float(small.mean()), float(small.std())
            covered = is_covered(luma, std, self.base_luma, c)
            if not covered:  # «обычную» яркость обновляем медленно и только по нормальным кадрам
                self.base_luma = luma if self.base_luma is None else 0.997 * self.base_luma + 0.003 * luma

            view = frame.copy()
            faces, res = 0, None
            phones, persons = [], []
            if covered:
                self.last_det = {"phones": [], "persons": []}
                self.p_phone.clear()
                self.p_raised.clear()
                self.p_multi.clear()
                cv2.putText(view, "CAMERA BLOCKED", (20, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                            (60, 80, 255), 3)
            else:
                if n % c.yolo_every == 1:
                    self.detect_yolo(frame)
                phones, persons = self.last_det["phones"], self.last_det["persons"]
                res = self.landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), int((now - t0) * 1000))
                faces = len(res.face_landmarks)

            # --- лицо / поза / взгляд ---
            face_bottom, calibrating = None, False
            gz = {k: 0.0 for k in GK}
            dyaw = dpitch = 0.0
            for i in range(faces):
                lm = res.face_landmarks[i]
                xs, ys = [p.x * w for p in lm], [p.y * h for p in lm]
                box = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
                cv2.rectangle(view, box[:2], box[2:], (0, 210, 120) if i == 0 else (60, 80, 255), 2)
                if i == 0:
                    face_bottom = box[3]
                    raw = self.gaze_scores(res.face_blendshapes[0])
                    yaw, pitch = head_angles(res.facial_transformation_matrixes[0])
                    if len(self.calib) < c.calib_frames:
                        calibrating = True
                        self.calib.append((yaw, pitch))
                        self.gcal.append([raw[k] for k in GK])
                        if len(self.calib) == c.calib_frames:
                            arr = np.array(self.calib)
                            self.base = (float(np.median(arr[:, 0])), float(np.median(arr[:, 1])))
                            g = np.median(np.array(self.gcal), axis=0)
                            self.gbase = {k: float(g[j]) for j, k in enumerate(GK)}
                    else:
                        dyaw, dpitch = wrap(yaw - self.base[0]), wrap(pitch - self.base[1])
                        gz = {k: max(0.0, raw[k] - self.gbase[k]) for k in GK}

            # --- телефон: подтверждение по времени (редкие кадры-ложные срабатывания не считаются) ---
            raised = False
            for box, conf in phones:
                limit = face_bottom if face_bottom is not None else 0.6 * h
                raised |= (box[1] + box[3]) / 2 < limit
                cv2.rectangle(view, tuple(map(int, box[:2])), tuple(map(int, box[2:])), (60, 80, 255), 2)
                cv2.putText(view, f"phone {conf:.2f}", (int(box[0]), max(14, int(box[1]) - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 80, 255), 2)
            phone_ok = self.p_phone.update(now, bool(phones))
            raised_ok = self.p_raised.update(now, raised)
            multi_ok = self.p_multi.update(now, faces >= 2 or len(persons) >= 2)

            if not self.risk.armed and (len(self.calib) >= c.calib_frames or now - t_calib0 > 15):
                self.risk.arm()  # риск стартует с 0% после калибровки
            gaze_ok = faces > 0 and not calibrating and not covered
            flags = {
                "phone": phone_ok, "raised": raised_ok, "cover": covered,
                "noface": faces == 0 and not covered, "multi": multi_ok,
                "down": gaze_ok and gz["down"] > c.gaze_down_thr,
                # «в стороны» теперь включает взгляд ВВЕРХ
                "side": gaze_ok and (max(gz["left"], gz["right"]) > c.gaze_side_thr or gz["up"] > c.gaze_up_thr),
                "head": gaze_ok and (abs(dyaw) > c.yaw_thr or abs(dpitch) > c.pitch_thr),
            }
            for key, active in flags.items():
                if self.risk.set(key, active):
                    self.emit(key, view)

            active_labels = [self.risk.sig[k].label for k, v in flags.items() if v]
            if not self.risk.armed:
                self.status, self.status_ok = tr("st.calib", n=len(self.calib), total=c.calib_frames), True
            elif active_labels:
                self.status, self.status_ok = " · ".join(active_labels[:2]), False
            else:
                self.status, self.status_ok = tr("st.ok"), True
            if c.debug:
                cv2.putText(view, f"L{luma:.0f} d{gz['down']:.2f} l{gz['left']:.2f} r{gz['right']:.2f} "
                                  f"u{gz['up']:.2f} yaw{dyaw:+.0f} pit{dpitch:+.0f}", (6, h - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
            if self.frames.full():
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    pass
            self.frames.put(view)
        cap.release()
        self.landmarker.close()


# --------------------------------------------------------------------------- #
# Защита окружения
# --------------------------------------------------------------------------- #
def foreground_pid() -> int | None:
    try:
        if sys.platform == "win32":
            hwnd = _u32.GetForegroundWindow()
            pid = wintypes.DWORD()
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value or None
        if sys.platform == "darwin":
            out = subprocess.check_output(
                ["osascript", "-e", 'tell application "System Events" to get unix id of '
                 'first process whose frontmost is true'], timeout=2, stderr=subprocess.DEVNULL)
            return int(out.strip())
        out = subprocess.check_output(["xdotool", "getactivewindow", "getwindowpid"],
                                      timeout=1, stderr=subprocess.DEVNULL)
        return int(out.strip())
    except Exception:  # noqa: BLE001
        return None


def find_browser() -> str | None:
    cands: list[Path] = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [Path(base) / "Google/Chrome/Application/chrome.exe",
                          Path(base) / "Microsoft/Edge/Application/msedge.exe"]
    elif sys.platform == "darwin":
        cands = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                 Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    else:
        cands = [Path(w) for n in ("google-chrome", "google-chrome-stable", "chromium",
                                   "chromium-browser", "microsoft-edge") if (w := shutil.which(n))]
    return next((str(c) for c in cands if c.exists()), None)


def launch_browser(url: str, x: int, y: int, w: int, h: int):
    """Окно браузера без вкладок и адресной строки, отдельный профиль без расширений."""
    exe = find_browser()
    if not exe:
        return None
    profile = Path("proctor_browser_profile").resolve()
    return subprocess.Popen([
        exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions",
        f"--window-position={x},{y}", f"--window-size={w},{h}"])


def process_tree(pid: int) -> set[int]:
    try:
        p = psutil.Process(pid)
        return {pid} | {c.pid for c in p.children(recursive=True)}
    except psutil.Error:
        return {pid}


class LayoutGuard(threading.Thread):
    """Windows: окно теста - без рамки, на левых 3/4 экрана, поверх всех окон. Любые попытки
    свернуть/сдвинуть/изменить размер откатываются. При завершении всё возвращается как было."""
    STRIP = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000  # caption|thickframe|sysmenu|min|max

    def __init__(self, browser: subprocess.Popen, rect: tuple[int, int, int, int],
                 crop: int = 0, extra_bottom: int = 0):
        super().__init__(daemon=True)
        # crop - высота верхней полосы Chrome (заголовок + меню «⋮»): окно сдвигается вверх за край
        # экрана на crop пикселей, полоса не видна и недоступна мышью, а страница занимает весь rect.
        # extra_bottom - запас снизу: окно выступает за нижний край экрана, чтобы не оставалось
        # полоски без браузера (Chrome может не занять последние пиксели высоты окна).
        self.browser, self.rect, self.crop, self.extra = browser, rect, crop, extra_bottom
        self.stop_flag = threading.Event()
        self.orig: dict[int, int] = {}

    @staticmethod
    def _signed(v: int) -> int:
        return v - 2 ** 32 if v >= 2 ** 31 else v

    def hwnds(self) -> list:
        pids = process_tree(self.browser.pid)
        found: list = []

        def cb(hwnd, _lparam):
            if _u32.IsWindowVisible(hwnd) and not _u32.GetWindow(hwnd, 4):  # без владельца
                pid = wintypes.DWORD()
                _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids:
                    r = wintypes.RECT()
                    _u32.GetWindowRect(hwnd, ctypes.byref(r))
                    if r.right - r.left > 300 and r.bottom - r.top > 200:
                        found.append(hwnd)
            return True

        _u32.EnumWindows(_EnumProc(cb), 0)
        return found

    def apply(self):
        x, y, w, h = self.rect
        y, h = y - self.crop, h + self.crop + self.extra
        for hwnd in self.hwnds():
            style = _u32.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF
            self.orig.setdefault(int(hwnd), style)
            changed = False
            if style & self.STRIP:
                _u32.SetWindowLongW(hwnd, -16, self._signed(style & ~self.STRIP))
                changed = True
            if _u32.IsIconic(hwnd):
                _u32.ShowWindow(hwnd, 9)  # SW_RESTORE
            r = wintypes.RECT()
            _u32.GetWindowRect(hwnd, ctypes.byref(r))
            topmost = _u32.GetWindowLongW(hwnd, -20) & 0x8
            if changed or (r.left, r.top, r.right, r.bottom) != (x, y, x + w, y + h) or not topmost:
                # HWND_TOPMOST | SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOACTIVATE
                _u32.SetWindowPos(hwnd, -1, x, y, w, h, 0x0020 | 0x0040 | 0x0010)

    def run(self):
        while not self.stop_flag.is_set():
            try:
                self.apply()
            except Exception:  # noqa: BLE001
                pass
            self.stop_flag.wait(0.7)

    def release(self):
        self.stop_flag.set()
        for hwnd, style in self.orig.items():
            try:
                _u32.SetWindowLongW(hwnd, -16, self._signed(style))
                # HWND_NOTOPMOST | NOMOVE | NOSIZE | FRAMECHANGED
                _u32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0020)
            except Exception:  # noqa: BLE001
                pass


class KeyboardGuard:
    BLOCK_KEYS = ["left windows", "right windows", "print screen", "menu"]
    BLOCK_HOTKEYS = [
        "alt+tab", "alt+shift+tab", "alt+esc", "ctrl+esc", "ctrl+shift+esc", "alt+f4",
        "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+insert", "shift+insert", "alt+print screen",
        "ctrl+tab", "ctrl+shift+tab", "ctrl+page up", "ctrl+page down",
        "ctrl+w", "ctrl+t", "ctrl+n", "ctrl+shift+n", "ctrl+l", "ctrl+shift+i", "f12", "ctrl+u",
    ] + [f"ctrl+{i}" for i in range(1, 10)]

    def __init__(self, on_block, on_info):
        self.on_block, self.on_info = on_block, on_info

    def start(self, emergency_exit):
        if keyboard is None:
            self.on_info(tr("guard.nomod"))
            return
        failed = 0
        try:
            for k in self.BLOCK_KEYS:
                try:
                    keyboard.block_key(k)
                except Exception:  # noqa: BLE001
                    failed += 1
            for hk in self.BLOCK_HOTKEYS:
                try:
                    keyboard.add_hotkey(hk, lambda h=hk: self.on_block(h), suppress=True)
                except Exception:  # noqa: BLE001
                    failed += 1
            keyboard.add_hotkey("ctrl+shift+f12", emergency_exit)
            if failed:
                self.on_info(tr("guard.failed", n=failed))
        except Exception as e:  # noqa: BLE001
            self.on_info(tr("guard.err", e=e))

    def stop(self):
        if keyboard is not None:
            try:
                keyboard.unhook_all()
            except Exception:  # noqa: BLE001
                pass


class WindowGuard(threading.Thread):
    """Разрешено только окно теста (дерево процессов браузера) и сама панель прокторинга."""
    def __init__(self, risk: RiskModel, notify, browser: subprocess.Popen):
        super().__init__(daemon=True)
        self.risk, self.notify, self.browser = risk, notify, browser
        self.stop_flag = threading.Event()
        self.closed = False

    def allowed(self) -> set[int]:
        return {os.getpid()} | process_tree(self.browser.pid)

    def run(self):
        while not self.stop_flag.is_set():
            if not self.risk.armed:
                self.stop_flag.wait(0.5)
                continue
            if self.browser.poll() is not None and not self.closed:
                self.closed = True
                self.risk.add("window", 3.0)
                self.notify("window", tr("win.closed"))
            fg = foreground_pid()
            if self.risk.set("window", fg is not None and fg not in self.allowed()):
                self.notify("window", MESSAGES["window"])
            self.stop_flag.wait(0.4)


class ProcessGuard(threading.Thread):
    """Поиск запрещённых программ.

    Режим с закрытием (kill=True):
      * самая первая зачистка (через ~2 с после старта) закрывает уже запущенные программы БЕЗ штрафа -
        в журнал пишется только информационная запись;
      * любая запрещённая программа, запущенная ПОСЛЕ этого (в том числе повторный запуск только что
        закрытой), закрывается и даёт штраф.
    Режим без закрытия: одно срабатывание со штрафом на каждое имя, пока программа не закроется.
    """
    FORBIDDEN = ("chrome", "msedge", "firefox", "opera", "brave", "vivaldi", "yandex", "safari",
                 "telegram", "discord", "whatsapp", "skype", "teams", "zoom", "anydesk",
                 "teamviewer", "rustdesk", "obs", "snippingtool", "screenclippinghost")

    def __init__(self, risk: RiskModel, notify, kill: bool, allowed_pids):
        super().__init__(daemon=True)
        self.risk, self.notify, self.kill, self.allowed_pids = risk, notify, kill, allowed_pids
        self.seen: set[str] = set()      # имена, за которые штраф уже начислен (пока программа жива)
        self.killed: set[int] = set()    # pid, которые мы уже закрыли (повторно не считаем)
        self.stop_flag = threading.Event()

    def sweep(self, initial: bool):
        ok = self.allowed_pids()
        present: dict[str, str] = {}  # нижний регистр без .exe -> настоящее имя
        for p in psutil.process_iter(["pid", "name"]):
            pid = p.info["pid"]
            raw = p.info["name"] or ""
            name = raw.lower().removesuffix(".exe")
            if pid in ok or not name.startswith(self.FORBIDDEN) or pid in self.killed:
                continue
            if self.kill:
                try:
                    p.kill()
                    self.killed.add(pid)
                except Exception:  # noqa: BLE001
                    pass
            present.setdefault(name, raw)

        if initial:
            # первая зачистка: закрываем, пишем в журнал, штраф НЕ начисляем
            for name, raw in present.items():
                self.notify("info", tr("app.initial", name=raw))
            self.seen |= set(present)
            return

        for name, raw in present.items():
            if name in self.seen:
                continue
            self.seen.add(name)
            self.risk.add("apps")
            self.notify("apps", tr("app.found", name=raw,
                                   closed=tr("app.closed") if self.kill else ""))
        # программа исчезла (закрыта) - следующий её запуск снова будет считаться нарушением
        self.seen &= set(present)

    def run(self):
        if self.kill:
            self.stop_flag.wait(2.0)  # дать браузеру теста запуститься, чтобы его процессы были в allowed
            if not self.stop_flag.is_set():
                try:
                    self.sweep(initial=True)
                except Exception:  # noqa: BLE001
                    pass
            self.stop_flag.wait(1.5)  # дать закрытым процессам исчезнуть
        while not self.stop_flag.is_set():
            if not self.risk.armed:
                self.stop_flag.wait(1)
                continue
            try:
                self.sweep(initial=False)
            except Exception:  # noqa: BLE001
                pass
            self.stop_flag.wait(4)


# --------------------------------------------------------------------------- #
# Голосовое / звуковое предупреждение
# --------------------------------------------------------------------------- #
VOICE_HINTS = {
    "ru": ("russian", "ru-ru", "ru_ru", "irina", "pavel", "ekaterina"),
    "kk": ("kazakh", "kk-kz", "kk_kz"),
    "en": ("english", "en-us", "en-gb", "en_us", "en_gb", "zira", "david", "hazel"),
}


def pick_voice(engine, lang: str) -> str | None:
    voices = engine.getProperty("voices") or []

    def match(hints):
        for v in voices:
            blob = f"{v.id} {v.name} {' '.join(map(str, getattr(v, 'languages', []) or []))}".lower()
            if any(h in blob for h in hints):
                return v.id
        return None

    # казахского голоса в Windows обычно нет - читаем русским, текст кириллицей
    return match(VOICE_HINTS[lang]) or (match(VOICE_HINTS["ru"]) if lang == "kk" else None)


class Speaker(threading.Thread):
    """Сигнал + фраза «Внимание, обнаружено нарушение! Вернитесь к тесту» (pyttsx3, офлайн)."""
    def __init__(self):
        super().__init__(daemon=True)
        self.q: queue.Queue = queue.Queue(maxsize=1)
        self.stop_flag = threading.Event()

    def say(self, text: str):
        try:
            self.q.put_nowait(text)
        except queue.Full:
            pass

    @staticmethod
    def beep():
        try:
            if winsound:
                winsound.Beep(1000, 150)
                winsound.Beep(1400, 220)
            else:
                print("\a", end="", flush=True)
        except Exception:  # noqa: BLE001
            pass

    def speak(self, text: str):
        if pyttsx3 is None:
            return
        try:
            eng = pyttsx3.init()
            vid = pick_voice(eng, LANG)
            if vid:
                eng.setProperty("voice", vid)
            eng.setProperty("rate", 160)
            eng.say(text)
            eng.runAndWait()
            eng.stop()
        except Exception:  # noqa: BLE001
            pass

    def run(self):
        if sys.platform == "win32":
            try:
                import comtypes
                comtypes.CoInitialize()
            except Exception:  # noqa: BLE001
                pass
        while not self.stop_flag.is_set():
            try:
                text = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            self.beep()
            self.speak(text)


class Alerter:
    """Не чаще раза в 6 с вообще; для «сильных» нарушений пауза 8 с, для остальных - 15 с."""
    def __init__(self, speaker: Speaker):
        self.sp = speaker
        self.last_any = 0.0
        self.last_tier = {"strong": 0.0, "weak": 0.0}

    def __call__(self, code: str):
        if code not in VOICE_CODES:
            return
        now = time.time()
        tier = "strong" if code in STRONG else "weak"
        gap = 8.0 if tier == "strong" else 15.0
        if now - self.last_any < 6.0 or now - self.last_tier[tier] < gap:
            return
        self.last_any = self.last_tier[tier] = now
        self.sp.say(tr("voice.warn"))


# --------------------------------------------------------------------------- #
# Виджеты
# --------------------------------------------------------------------------- #
class Gauge(tk.Canvas):
    def __init__(self, master, w=350, h=140, th=18, big=36):
        super().__init__(master, width=w, height=h, bg=CARD, highlightthickness=0)
        self.w, self.h, self.th, self.big = w, h, th, big

    def draw(self, p: float, caption: str):
        self.delete("all")
        cx, cy = self.w / 2, self.h - max(24, self.h * 0.16)
        r = min(self.w / 2 - 22, cy - 18)
        box = (cx - r, cy - r, cx + r, cy + r)
        self.create_arc(*box, start=0, extent=180, style="arc", width=self.th, outline=TRACK)
        if p > 0.004:
            self.create_arc(*box, start=180, extent=-180 * p, style="arc",
                            width=self.th, outline=risk_color(p))
        self.create_text(cx, cy - r * 0.34, text=f"{p * 100:.0f}%", fill=TEXT,
                         font=("Arial", self.big, "bold"))
        self.create_text(cx, cy + 12, text=caption, fill=risk_color(p), font=("Arial", 12, "bold"))


def draw_spark(c: tk.Canvas, hist: list[float], w: int, h: int):
    c.delete("all")
    for g in (0.25, 0.5, 0.75):
        y = h - 6 - g * (h - 12)
        c.create_line(4, y, w - 4, y, fill=TRACK, dash=(2, 4))
    if len(hist) < 2:
        return
    step = max(1, len(hist) // 300)
    pts_src = hist[::step]
    n, pts = len(pts_src), []
    for i, v in enumerate(pts_src):
        pts += [4 + i * (w - 8) / (n - 1), h - 6 - v * (h - 12)]
    c.create_line(*pts, fill=ACCENT, width=2, smooth=True)


def open_folder(path: Path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def fmt_time(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #
class App(ctk.CTk):
    def __init__(self, args):
        super().__init__(fg_color=BG)
        self.args = args
        self.title("Qostanai Proctor")
        self.running = self.finished = False
        self.key_last: dict[str, float] = {}
        self.ui_events: queue.Queue = queue.Queue()
        self._form: dict = {}
        self._tick = 0
        self.alerter = self.speaker = self.layout = self.wg = self.browser = None
        self.logwin = self.logbox = None
        self.log_entries: list[tuple[str, str, str]] = []
        self.lock_mode = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build_start()

    # ---------- утилиты ---------- #
    def scale(self) -> float:
        try:
            return float(self._get_window_scaling())
        except Exception:  # noqa: BLE001
            return 1.0

    def fit_start(self, w: int = 460, h: int = 700):
        h = min(h, int(self.winfo_screenheight() / self.scale()) - 70)
        self.geometry(f"{w}x{h}")
        self.resizable(False, False)

    def clear(self):
        for w in self.winfo_children():
            w.destroy()

    def collect_form(self):
        self._form = {"name": self.name_e.get(), "url": self.url_e.get(), "keys": self.v_keys.get(),
                      "kill": self.v_kill.get(), "voice": self.v_voice.get()}

    # ---------- стартовый экран ---------- #
    def build_start(self):
        form = self._form
        self.clear()
        self.fit_start()

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=26, pady=(14, 0))
        seg = ctk.CTkSegmentedButton(top, values=list(LANG_LABELS.values()), command=self.on_lang,
                                     fg_color=CARD2, selected_color=ACCENT, selected_hover_color="#5575e6",
                                     unselected_color=CARD2, unselected_hover_color=TRACK)
        seg.set(LANG_LABELS[LANG])
        seg.pack(side="right")

        ctk.CTkLabel(self, text="◉  Qostanai Proctor", font=ctk.CTkFont(size=28, weight="bold"),
                     text_color=TEXT).pack(pady=(14, 0))
        ctk.CTkLabel(self, text=tr("start.subtitle"), text_color=MUTED).pack(pady=(2, 14))

        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=18)
        card.pack(fill="x", padx=26)
        ctk.CTkLabel(card, text=tr("start.name"), text_color=MUTED, anchor="w").pack(fill="x", padx=20, pady=(16, 2))
        self.name_e = ctk.CTkEntry(card, height=40, corner_radius=10, fg_color=CARD2,
                                   border_width=0, placeholder_text=tr("start.name_ph"))
        self.name_e.pack(fill="x", padx=20)
        ctk.CTkLabel(card, text=tr("start.url"), text_color=MUTED,
                     anchor="w").pack(fill="x", padx=20, pady=(12, 2))
        self.url_e = ctk.CTkEntry(card, height=40, corner_radius=10, fg_color=CARD2,
                                  border_width=0, placeholder_text="https://docs.google.com/forms/…")
        self.url_e.pack(fill="x", padx=20)
        if form.get("name"):
            self.name_e.insert(0, form["name"])
        if form.get("url"):
            self.url_e.insert(0, form["url"])

        self.v_keys = tk.IntVar(value=form.get("keys", 0 if self.args.safe else 1))
        self.v_kill = tk.IntVar(value=form.get("kill", 1 if self.args.kill_forbidden else 0))
        self.v_voice = tk.IntVar(value=form.get("voice", 1))
        for text, var in ((tr("start.sw_keys"), self.v_keys), (tr("start.sw_kill"), self.v_kill),
                          (tr("start.sw_voice"), self.v_voice)):
            ctk.CTkSwitch(card, text=text, variable=var, progress_color=ACCENT,
                          text_color=TEXT).pack(anchor="w", padx=22, pady=(12, 0))
        ctk.CTkLabel(card, text="", height=6).pack()

        ctk.CTkLabel(self, text=tr("start.info"), text_color=MUTED, justify="left").pack(anchor="w", padx=34, pady=14)
        ctk.CTkButton(self, text=tr("start.btn"), height=50, corner_radius=14,
                      fg_color=ACCENT, hover_color="#5575e6",
                      font=ctk.CTkFont(size=16, weight="bold"),
                      command=self.show_rules).pack(fill="x", padx=26)

    def on_lang(self, label: str):
        global LANG
        LANG = next(k for k, v in LANG_LABELS.items() if v == label)
        self.collect_form()
        self.build_start()

    # ---------- инструкция ---------- #
    def show_rules(self):
        self.collect_form()
        self.build_rules()

    def build_rules(self):
        self.clear()
        self.fit_start()
        ctk.CTkLabel(self, text=tr("rules.title"), font=ctk.CTkFont(size=24, weight="bold"),
                     text_color=TEXT).pack(pady=(20, 8))
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=18)
        card.pack(fill="x", padx=22)
        for i in range(1, 9):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(8 if i == 1 else 4, 8 if i == 8 else 4))
            ctk.CTkLabel(row, text=str(i), width=26, height=26, corner_radius=13, fg_color=ACCENT,
                         text_color="white", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", anchor="n")
            ctk.CTkLabel(row, text=tr(f"rules.{i}"), text_color=TEXT, justify="left", anchor="w",
                         wraplength=350).pack(side="left", padx=10, fill="x", expand=True)
        self.agree = tk.IntVar(value=0)
        ctk.CTkCheckBox(self, text=tr("rules.agree"), variable=self.agree, command=self._toggle_go,
                        fg_color=ACCENT, text_color=TEXT).pack(anchor="w", padx=26, pady=(14, 8))
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=22, pady=(0, 12))
        ctk.CTkButton(btns, text=tr("rules.back"), height=46, corner_radius=12, fg_color=CARD2,
                      hover_color=TRACK, command=self.build_start, width=110).pack(side="left", padx=(0, 8))
        self.go_btn = ctk.CTkButton(btns, text=tr("start.go"), height=46, corner_radius=12, fg_color=ACCENT,
                                    hover_color="#5575e6", font=ctk.CTkFont(size=15, weight="bold"),
                                    state="disabled", command=self.begin_session)
        self.go_btn.pack(side="left", fill="x", expand=True)

    def _toggle_go(self):
        self.go_btn.configure(state="normal" if self.agree.get() else "disabled")

    def begin_session(self):
        self.go_btn.configure(text=tr("start.loading"), state="disabled")
        self.after(60, self.start_session)

    # ---------- запуск сеанса ---------- #
    def start_session(self):
        a, f = self.args, self._form
        self.student = (f.get("name") or "").strip() or tr("default.student")
        url = (f.get("url") or "").strip()
        if url and not url.startswith("http"):
            url = "https://" + url
        self.url = url
        self.risk, self.log = RiskModel(), EventLog()

        if f.get("voice", 1):
            self.speaker = Speaker()
            self.speaker.start()
            self.alerter = Alerter(self.speaker)
            if pyttsx3 is None:
                self.notify("info", tr("guard.novoice"))
        self.engine = ProctorEngine(Cfg(debug=a.debug), self.log, self.risk, a.camera, a.yolo, a.device,
                                    self.alerter)
        self.engine.start()

        # экран делится 3:1 - слева тест, справа панель
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.lock_mode = not a.safe
        self.PW = sw // 4
        self.BW = sw - self.PW
        if self.lock_mode:
            self.PH = sh  # боевой режим: на весь экран, панель задач перекрыта
        else:
            wa = work_area()  # отладка: реальная рабочая область без панели задач
            self.PH = wa[3] if wa else sh - 80
        self.browser = None
        if url:
            self.browser = launch_browser(url, 0, 0, self.BW, self.PH)
            if self.browser is None:
                messagebox.showwarning(tr("warn.title"), tr("warn.browser"))
        if self.browser and self.lock_mode and sys.platform == "win32":
            self.layout = LayoutGuard(self.browser, (0, 0, self.BW, self.PH),
                                      crop=int(BROWSER_BAR * self.scale()),
                                      extra_bottom=int(BOTTOM_OVERSCAN * self.scale()))
            self.layout.start()

        self.kb = KeyboardGuard(self.on_key, lambda m: self.notify("info", m))
        if f.get("keys", 1):
            self.kb.start(lambda: self.after(0, self.finish, True))
        self.wg = WindowGuard(self.risk, self.notify, self.browser) if self.browser else None
        if self.wg:
            self.wg.start()
        self.pg = ProcessGuard(
            self.risk, self.notify, bool(f.get("kill", 0)),
            lambda: ({os.getpid()} | process_tree(self.browser.pid)) if self.browser else {os.getpid()})
        self.pg.start()

        self.t_start = time.time()
        self.running = True
        self.clear()
        self.build_monitor()
        self.update_idletasks()
        self.withdraw()
        if self.lock_mode:
            self.overrideredirect(True)  # без рамки: панель нельзя свернуть/закрыть/сдвинуть
        self.resizable(False, False)
        tk.Tk.geometry(self, f"{self.PW}x{self.PH}+{self.BW}+0")  # реальные пиксели
        self.attributes("-topmost", True)
        self.deiconify()
        self.lift()
        self.after(40, self.poll)

    # ---------- рабочая панель ---------- #
    def build_monitor(self):
        k = self.scale()
        lw = self.PW / k  # ширина панели в «логических» единицах CTk
        cam_w = int(min(300, lw - 56))
        self.cam_size = (cam_w, cam_w * 3 // 4)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(head, text="●", text_color=GREEN_HEX, font=ctk.CTkFont(size=14)).pack(side="left")
        ctk.CTkLabel(head, text=f" {self.student}", text_color=TEXT,
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        self.timer_lbl = ctk.CTkLabel(head, text="00:00", text_color=MUTED)
        self.timer_lbl.pack(side="right", padx=8)

        gcard = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        gcard.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(gcard, text=tr("mon.title"), text_color=MUTED,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(pady=(10, 0))
        self.gauge = Gauge(gcard, w=max(200, int((lw - 44) * k)), h=int(135 * k), th=int(16 * k))
        self.gauge.pack(pady=(0, 8))

        ccard = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        ccard.pack(fill="x", padx=12, pady=4)
        self.cam_lbl = ctk.CTkLabel(ccard, text="", width=self.cam_size[0], height=self.cam_size[1])
        self.cam_lbl.pack(padx=10, pady=(10, 4))
        self.status_lbl = ctk.CTkLabel(ccard, text=tr("st.loading"), text_color=MUTED,
                                       font=ctk.CTkFont(size=13, weight="bold"), wraplength=int(lw - 60))
        self.status_lbl.pack(pady=(0, 10))

        tf = ctk.CTkScrollableFrame(self, height=150, fg_color=CARD, corner_radius=16)
        tf.pack(fill="both", expand=True, padx=12, pady=4)
        self.rows = {}
        for key, *_ in SIGNALS:
            row = ctk.CTkFrame(tf, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=tr("sig." + key), width=180, anchor="w", text_color=TEXT,
                         font=ctk.CTkFont(size=12)).pack(side="left")
            bar = ctk.CTkProgressBar(row, height=8, fg_color=TRACK, progress_color=GREEN_HEX)
            bar.set(0)
            bar.pack(side="left", fill="x", expand=True, padx=6)
            val = ctk.CTkLabel(row, text="0%", width=36, anchor="e", text_color=MUTED,
                               font=ctk.CTkFont(size=12))
            val.pack(side="right")
            self.rows[key] = (bar, val)
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkButton(btns, text=tr("tab.log"), height=42, corner_radius=12,
                      fg_color=ACCENT, hover_color="#5575e6", text_color="white",
                      command=self.open_logs).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(btns, text=tr("mon.finish"), height=42, corner_radius=12,
                      fg_color=CARD2, hover_color=TRACK, text_color=TEXT,
                      command=self.finish).pack(side="left", fill="x", expand=True, padx=(4, 0))

    # ---------- журнал ---------- #
    def _log_insert(self, box, t: str, text: str, tag: str):
        box.configure(state="normal")
        box.insert("end", f"{t}  ", "t")
        box.insert("end", f"{text}\n", tag)
        box.see("end")
        box.configure(state="disabled")

    def open_logs(self):
        if self.logwin is not None and self.logwin.winfo_exists():
            self.logwin.lift()
            return
        win = tk.Toplevel(self, bg=BG)
        if self.lock_mode:
            win.overrideredirect(True)  # поверх панели, без рамки
        win.title(tr("tab.log"))
        tk.Toplevel.geometry(win, f"{self.PW}x{self.PH}+{self.BW}+0")  # реальные пиксели
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close_logs)
        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(head, text=tr("tab.log"), text_color=TEXT,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        ctk.CTkButton(head, text=tr("rep.close"), width=90, height=32, corner_radius=10,
                      fg_color=CARD2, hover_color=TRACK, text_color=TEXT,
                      command=self.close_logs).pack(side="right")
        box = ctk.CTkTextbox(win, fg_color=CARD2, text_color=TEXT, wrap="word",
                             font=ctk.CTkFont(size=12))
        box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        box.tag_config("hi", foreground="#ff5470")
        box.tag_config("mid", foreground="#ff9f43")
        box.tag_config("t", foreground=MUTED)
        box.configure(state="disabled")
        for t, text, tag in self.log_entries:
            self._log_insert(box, t, text, tag)
        self.logwin, self.logbox = win, box

    def close_logs(self):
        if self.logwin is not None:
            try:
                self.logwin.destroy()
            except Exception:  # noqa: BLE001
                pass
        self.logwin = self.logbox = None

    # ---------- события ---------- #
    def notify(self, code: str, text: str):
        self.ui_events.put(self.log.write(code, text))
        if self.alerter and code in VOICE_CODES:
            self.alerter(code)

    def on_key(self, hk: str):
        if not self.risk.armed:
            return
        now = time.time()
        if now - self.key_last.get(hk, 0) > 1.0:
            self.key_last[hk] = now
            self.risk.add("keys")
            self.notify("keys", tr("key.try", hk=hk.upper()))

    def add_log(self, evt: dict):
        code = evt["code"]
        self.risk.counts[code] += 1
        tag = "hi" if code in STRONG else "mid"
        entry = (evt["time"][11:], evt["text"], tag)
        self.log_entries.append(entry)
        if self.logwin is not None and self.logwin.winfo_exists():
            self._log_insert(self.logbox, *entry)

    def poll(self):
        if not self.running:
            return
        self._tick += 1
        if self.lock_mode and self._tick % 25 == 0:  # держим панель поверх всех окон
            self.attributes("-topmost", True)
            self.lift()
            if self.logwin is not None and self.logwin.winfo_exists():
                self.logwin.attributes("-topmost", True)
                self.logwin.lift()
        p, lv, contrib = self.risk.snapshot()
        self.gauge.draw(p, level_text(p))
        for key, (bar, val) in self.rows.items():
            bar.set(lv[key])
            bar.configure(progress_color=risk_color(lv[key]))
            val.configure(text=f"{contrib[key] * 100:.0f}%")
        self.timer_lbl.configure(text=fmt_time(time.time() - self.t_start))
        try:
            fr = self.engine.frames.get_nowait()
            img = Image.fromarray(cv2.cvtColor(cv2.resize(fr, self.cam_size), cv2.COLOR_BGR2RGB))
            self.cam_img = ctk.CTkImage(light_image=img, dark_image=img, size=self.cam_size)
            self.cam_lbl.configure(image=self.cam_img)
        except queue.Empty:
            pass
        self.status_lbl.configure(text=self.engine.status,
                                  text_color=GREEN_HEX if self.engine.status_ok else RED_HEX)
        for q in (self.engine.events, self.ui_events):
            while True:
                try:
                    self.add_log(q.get_nowait())
                except queue.Empty:
                    break
        self.after(40, self.poll)

    # ---------- завершение ---------- #
    def on_close(self):
        if self.running:
            self.finish()
        else:
            self.destroy()

    def finish(self, forced: bool = False):
        if self.finished or not self.running:
            return
        if not forced and not messagebox.askyesno(
                tr("confirm.title"), tr("confirm.text"), parent=self):
            return
        self.finished, self.running = True, False
        self.close_logs()
        self.engine.stop_flag.set()
        self.pg.stop_flag.set()
        if self.wg:
            self.wg.stop_flag.set()
        if self.speaker:
            self.speaker.stop_flag.set()
        if self.layout:
            self.layout.release()  # вернуть рамку и убрать topmost у окна теста
        self.kb.stop()
        summary = self.make_summary()
        self.log.write("finish", tr("fin.log", p=summary["final_percent"]))
        self.log.close()
        (self.log.dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.build_report(summary)

    def make_summary(self) -> dict:
        p, _, _ = self.risk.snapshot()
        peak = self.risk.peak
        final = max(p, 0.7 * peak)  # устойчивый пик (>= 4 с) учитывается на 70%, короткие всплески - нет
        hist = self.risk.hist
        return {
            "student": self.student, "url": self.url,
            "duration_sec": round(time.time() - self.t_start),
            "final_percent": round(final * 100), "peak_percent": round(peak * 100),
            "average_percent": round(100 * sum(hist) / len(hist)) if hist else 0,
            "factors": {k: {"label": s.label, "active_sec": round(s.active_time, 1),
                            "events": self.risk.counts.get(k, 0)} for k, s in self.risk.sig.items()},
            "history": [round(v, 3) for v in hist[::max(1, len(hist) // 300)]],
        }

    def build_report(self, s: dict):
        self.clear()
        self.withdraw()
        self.overrideredirect(False)
        self.attributes("-topmost", False)
        self.resizable(False, False)
        self.geometry(f"520x{min(760, int(self.winfo_screenheight() / self.scale()) - 80)}+80+10")
        self.deiconify()

        ctk.CTkLabel(self, text=tr("rep.title"), font=ctk.CTkFont(size=24, weight="bold"),
                     text_color=TEXT).pack(pady=(18, 0))
        ctk.CTkLabel(self, text=f"{s['student']} · {fmt_time(s['duration_sec'])}",
                     text_color=MUTED).pack(pady=(0, 8))

        gcard = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        gcard.pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(gcard, text=tr("rep.final"), text_color=MUTED,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(pady=(10, 0))
        g = Gauge(gcard, w=440, h=200, th=22, big=52)
        g.pack(pady=(0, 8))
        f = s["final_percent"] / 100
        g.draw(f, level_text(f))

        stats = ctk.CTkFrame(self, fg_color="transparent")
        stats.pack(fill="x", padx=20, pady=4)
        for i, (title, val) in enumerate(((tr("rep.peak"), f"{s['peak_percent']}%"),
                                          (tr("rep.avg"), f"{s['average_percent']}%"),
                                          (tr("rep.events"), str(sum(v['events'] for v in s['factors'].values()))))):
            c = ctk.CTkFrame(stats, fg_color=CARD, corner_radius=14)
            c.grid(row=0, column=i, sticky="nsew", padx=4)
            stats.columnconfigure(i, weight=1)
            ctk.CTkLabel(c, text=val, font=ctk.CTkFont(size=22, weight="bold"), text_color=TEXT).pack(pady=(8, 0))
            ctk.CTkLabel(c, text=title, text_color=MUTED).pack(pady=(0, 8))

        sc = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        sc.pack(fill="x", padx=20, pady=6)
        spark = tk.Canvas(sc, width=440, height=80, bg=CARD, highlightthickness=0)
        spark.pack(padx=10, pady=10)
        draw_spark(spark, s["history"], 440, 80)

        fc = ctk.CTkScrollableFrame(self, fg_color=CARD, corner_radius=16)
        fc.pack(fill="both", expand=True, padx=20, pady=4)
        shown = [(v["label"], v["active_sec"], v["events"]) for v in s["factors"].values()
                 if v["active_sec"] > 0 or v["events"] > 0]
        if not shown:
            ctk.CTkLabel(fc, text=tr("rep.clean"), text_color=GREEN_HEX).pack(pady=20)
        for label, sec, ev in shown:
            r = ctk.CTkFrame(fc, fg_color="transparent")
            r.pack(fill="x", padx=8, pady=3)
            ctk.CTkLabel(r, text=label, text_color=TEXT, anchor="w").pack(side="left")
            ctk.CTkLabel(r, text=tr("rep.row", sec=sec, ev=ev), text_color=MUTED).pack(side="right")
        ctk.CTkLabel(self, text=tr("rep.note"), text_color=MUTED, font=ctk.CTkFont(size=11),
                     wraplength=470).pack(pady=2)
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(4, 14))
        ctk.CTkButton(btns, text=tr("rep.open"), fg_color=ACCENT, hover_color="#5575e6",
                      command=lambda: open_folder(self.log.dir)).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(btns, text=tr("rep.close"), fg_color=CARD2, hover_color=TRACK,
                      command=self.destroy).pack(side="left", expand=True, fill="x", padx=(4, 0))


def main():
    global LANG
    ap = argparse.ArgumentParser(description="Qostanai Proctor")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--lang", choices=["ru", "kk", "en"], default="ru", help="язык интерфейса по умолчанию")
    ap.add_argument("--yolo", default="yolo11n.pt", help="yolo11n.pt или yolov8n.pt")
    ap.add_argument("--device", default=None, help="cpu / cuda:0 / mps")
    ap.add_argument("--safe", action="store_true", help="отладка: обычные окна, клавиши не блокируются")
    ap.add_argument("--kill-forbidden", action="store_true", help="закрывать запрещённые программы")
    ap.add_argument("--debug", action="store_true", help="показывать метрики взгляда на кадре")
    args = ap.parse_args()
    LANG = args.lang
    ctk.set_appearance_mode("dark")
    App(args).mainloop()


if __name__ == "__main__":
    main()