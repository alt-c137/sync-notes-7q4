"""
Бот «Новости исламского мира» для канала @ilm4_info.

Две части, обе работают на серверах — компьютер не нужен:

  А) ДЕЖУРНЫЙ — `python bot.py` (GitHub Actions, каждые 30 минут, без ИИ):
     - забирает готовые черновики от редактора и шлёт их в группу модерации;
     - обрабатывает кнопки ✅ ❌ 🕒 💧 и команды (/night, /auto, /gap …);
     - публикует в канал: по одной, чередуя добрые и тяжёлые новости,
       отложенные — точно ко времени, ночью — проверенные Claude (автопилот);
     - ставит водяной знак на фото и видео.

  Б) РЕДАКТОР — Claude находит новости, переводит и пишет посты:
     1. Чат Claude в облаке (routine по подписке, без API) по заданию zadanie.md:
          python bot.py fetch             -> свежие новости в candidates.json
          python bot.py article 3 17 25   -> тексты выбранных статей
          python bot.py send posts.json   -> черновики в ветку claude/inbox
     2. Или API: если задан ANTHROPIC_API_KEY, дежурный делает всё это сам.
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import quote, urlparse

import feedparser
import requests
import trafilatura

os.chdir(os.path.dirname(os.path.abspath(__file__)))   # все файлы — рядом с bot.py

# ================== НАСТРОЙКИ (меняй здесь) ==================


def gnews(query, lang):
    """RSS поиска Google News. lang: ru / en / ar / sa (арабский, Саудия)."""
    loc = {"ru": ("ru", "RU", "RU:ru"), "en": ("en-US", "US", "US:en"), "gb": ("en-GB", "GB", "GB:en"),
           "fr": ("fr", "FR", "FR:fr"), "de": ("de", "DE", "DE:de"),
           "ar": ("ar", "EG", "EG:ar"), "sa": ("ar", "SA", "SA:ar")}[lang]
    return (f"https://news.google.com/rss/search?q={quote(query + ' when:1d')}"
            f"&hl={loc[0]}&gl={loc[1]}&ceid={loc[2]}")


# Западные и англоязычные издания пишут обо всём — берём у них только то, что касается нашего региона и мусульман
WEST_TOPIC = ("(Saudi OR Iran OR Syria OR Yemen OR Houthi OR Houthis OR Gaza OR Iraq OR Lebanon OR Muslim OR Muslims "
              "OR Islam OR Islamic OR mosque OR Mecca OR Hajj OR Sudan OR Afghanistan OR Hormuz OR Uzbekistan OR Tajikistan "
              "OR hijab OR niqab OR burqa)")

# Мусульмане по всему миру: поиск Google News по всем изданиям на пяти языках (как у учителя).
# Источником такой новости считается само издание, которое её написало.
WORLD_SEARCH = "🌍 Поиск"
WORLD = [
    (f"{WORLD_SEARCH} (англ.)", gnews("(Muslims OR Muslim OR Islam OR mosque OR hijab OR niqab OR burqa OR Islamophobia "
                                      "OR imam OR Quran OR halal OR madrasa)", "en")),
    (f"{WORLD_SEARCH} (Британия)", gnews("(Muslims OR mosque OR hijab OR niqab OR Islamophobia OR imam OR Quran)", "gb")),
    (f"{WORLD_SEARCH} (франц.)", gnews("(musulmans OR musulmane OR mosquée OR \"voile islamique\" OR hijab OR niqab OR abaya "
                                       "OR islam OR imam)", "fr")),
    (f"{WORLD_SEARCH} (нем.)", gnews("(Muslime OR Moschee OR Kopftuch OR Islam OR Imam OR Burka)", "de")),
    (f"{WORLD_SEARCH} (рус.)", gnews("(мусульмане OR мусульман OR мечеть OR хиджаб OR никаб OR ислам OR имам "
                                     "OR Коран OR муфтий)", "ru")),
    (f"{WORLD_SEARCH} (араб.)", gnews("(المسلمين OR مسجد OR الحجاب OR النقاب OR الإسلاموفوبيا OR \"الجالية المسلمة\")",
                                      "ar")),
]


def west(site):
    return gnews(f"site:{site} {WEST_TOPIC}", "en")


# Источники: сайт (RSS или поиск Google News по сайту) + Telegram-канал, где он есть.
# Формат: (название, RSS-ссылка), (название, "tg:имя_канала") или (название, "x:аккаунт_в_X").
# Список собран по ссылкам «Источник» в @ilm4_info (22–25 сентября) и советам шейха.
FEEDS = [
    # 🇸🇦 Саудовская Аравия
    ("SaudiNews50", "tg:SaudiNews50"),
    ("SaudiNews50", "x:SaudiNews50"),
    ("SPA (агентство КСА)", gnews("site:spa.gov.sa", "sa")),
    ("Sabq", gnews("site:sabq.org", "sa")),
    ("Twasul", gnews("site:twaslnews.com", "sa")),
    ("Okaz", "https://okaz.com.sa/rssFeed/1"),
    ("Al Riyadh", gnews("site:alriyadh.com", "sa")),
    ("Asharq Al-Awsat", "https://aawsat.com/feed"),
    ("Asharq News", "https://asharq.com/rss.xml"),
    ("Asharq News", "tg:AsharqNews"),
    ("Al Arabiya", gnews("site:alarabiya.net", "sa")),
    ("Al Arabiya", "tg:AlArabiya"),
    ("Al-Eqtisad", "https://aleqtsad.org/rss"),
    # 🕌 Официальные религиозные источники КСА (проверенные, саляфитские)
    ("Министерство исламских дел КСА", gnews("site:moia.gov.sa", "sa")),
    ("Всемирная исламская лига", gnews("site:themwl.org", "sa")),
    ("Харамайн", gnews('"رئاسة الشؤون الدينية" OR "إمام المسجد الحرام" OR "إمام المسجد النبوي" OR '
                       '"خطيب المسجد الحرام" OR "خطيب المسجد النبوي"', "sa")),
    # 🇦🇪 🇰🇼 Залив
    ("Sky News Arabia", "https://skynewsarabia.com/rss"),
    ("Al Khaleej", gnews("site:alkhaleej.ae", "sa")),
    ("Al Mashhad", gnews("site:almashhad.com", "sa")),
    ("Al Rai (Кувейт)", "https://alraimedia.com/rssFeed/1"),
    # 🏛 Официальные агентства арабских стран — первоисточник заявлений своих властей
    ("WAM (агентство ОАЭ)", gnews("site:wam.ae", "sa")),
    ("KUNA (агентство Кувейта)", gnews("site:kuna.net.kw", "sa")),
    ("QNA (агентство Катара)", gnews("site:qna.org.qa", "sa")),
    ("BNA (агентство Бахрейна)", gnews("site:bna.bh", "sa")),
    ("ONA (агентство Омана)", gnews("site:omannews.gov.om", "sa")),
    ("Petra (агентство Иордании)", gnews("site:petra.gov.jo", "sa")),
    ("MENA (агентство Египта)", gnews("site:mena.org.eg", "sa")),
    ("NNA (агентство Ливана)", gnews("site:nna-leb.gov.lb", "sa")),
    ("MAP (агентство Марокко)", gnews("site:mapnews.ma", "sa")),
    ("SUNA (агентство Судана)", gnews("site:suna-sd.net", "sa")),
    ("APS (агентство Алжира)", gnews("site:aps.dz", "sa")),
    # 🇸🇾 Сирия
    ("SANA (агентство Сирии)", "tg:Sana_gov"),
    ("SANA (агентство Сирии)", "https://sana.sy/feed/"),
    ("SOHR", gnews("site:syriahr.com", "sa")),
    # 🇮🇶 Ирак
    ("INA (агентство Ирака)", gnews("site:ina.iq", "sa")),
    # 🇾🇪 Йемен (законное правительство)
    ("Saba (правительство Йемена)", gnews("site:sabanew.net", "sa")),
    ("Al-Masdar Online", "tg:almasdaronline"),
    ("Al-Masdar Online", "https://almasdaronline.com/rss"),
    # 🇱🇧 Ливан
    ("Annahar", "https://annahar.com/rss"),
    ("Lebanon Debate", "tg:lebanondebate"),
    ("Lebanon Debate", gnews("site:lebanondebate.com", "sa")),
    ("Al Markazia", "https://almarkazia.com/ar/rss"),
    ("Sawt Beirut", "tg:sawtbeirut"),
    ("Sawt Beirut", gnews("site:sawtbeirut.com", "sa")),
    ("Voice of Lebanon", gnews("site:vdlnews.com", "sa")),
    # 🇪🇬 🇯🇴 🇵🇸 🇩🇿 Египет, Иордания, Палестина, Алжир
    ("Youm7", gnews("site:youm7.com", "sa")),
    ("Shorouk", gnews("site:shorouknews.com", "sa")),
    ("El Balad", "tg:elbaladnews"),
    ("Akhbar El Yom", gnews("site:akhbaralyawm.com", "sa")),
    ("El Aosboa", gnews("site:elaosboa.com", "sa")),
    ("Egypt Telegraph", gnews("site:egypttelegraph.com", "sa")),
    ("Jordan Zad", gnews("site:jordanzad.com", "sa")),
    ("Madar News", "https://madar.news/rss"),
    ("El Djazair El Djadida", gnews("site:eldjazaireldjadida.dz", "sa")),
    # 🇺🇿 Узбекистан
    ("Kun.uz", "tg:kunuzofficial"),
    ("Kun.uz", "https://kun.uz/news/rss"),
    ("Gazeta.uz", "tg:gazetauz"),
    ("Gazeta.uz", "https://www.gazeta.uz/ru/rss/"),
    ("Daryo", "tg:daryo"),
    ("UzA (агентство Узбекистана)", "https://uza.uz/ru/rss"),
    # 🇹🇯 Таджикистан
    ("Азия-Плюс", "tg:asiaplustj"),
    ("Ховар (агентство Таджикистана)", "https://khovar.tj/rus/feed/"),
    # 🇰🇿 🇰🇬 Казахстан, Кыргызстан
    ("Tengrinews", "https://tengrinews.kz/news.rss"),
    ("24.kg", "https://24.kg/rss/"),
    ("Kaktus Media", "tg:kaktus_media"),
    # 🇹🇷 Турция
    ("Анадолу", "https://www.aa.com.tr/ru/rss/default?cat=guncel"),
    # 🇵🇰 🇮🇩 🇲🇾 Пакистан, Индонезия, Малайзия
    ("Dawn (Пакистан)", "https://www.dawn.com/feeds/home"),
    ("Antara (Индонезия)", "https://en.antaranews.com/rss/news.xml"),
    ("Bernama (Малайзия)", "https://www.bernama.com/en/rssfeed.php"),
    # Ещё из источников учителя (надёжные)
    ("Independent Arabia", gnews("site:independentarabia.com", "sa")),
    ("Enab Baladi (Сирия)", gnews("site:enabbaladi.net", "sa")),
    ("Al-Ahram", gnews("site:ahram.org.eg", "sa")),
    ("Erem News", gnews("site:eremnews.com", "sa")),
    # 🌍 Международные (арабские службы)
    ("Monte Carlo Doualiya", gnews("site:mc-doualiya.com", "sa")),
    ("Euronews Arabic", "https://arabic.euronews.com/rss"),
    ("InfoMigrants", gnews("site:infomigrants.net", "sa")),
    ("Hormuz Report", "x:HormuzReport"),
    # 🌐 Мировые агентства — первоисточник мировых новостей (самые достоверные)
    ("Reuters", west("reuters.com")),
    ("AP", west("apnews.com")),
    ("AFP", west("afp.com")),
    # 🇸🇦 Саудовские издания на английском
    ("Arab News", west("arabnews.com")),
    ("Saudi Gazette", west("saudigazette.com.sa")),
    # 🇺🇸 🇬🇧 🇪🇺 Крупные западные издания (факты надёжные, но у некоторых свой взгляд на регион — решает модератор)
    ("Axios", west("axios.com")),
    ("BBC", west("bbc.com")),
    ("The Guardian", west("theguardian.com")),
    ("New York Times", west("nytimes.com")),
    ("Wall Street Journal", west("wsj.com")),
    ("Financial Times", west("ft.com")),
    ("Bloomberg", west("bloomberg.com")),
    ("France 24", west("france24.com")),
    ("DW", west("dw.com")),
    ("The National (ОАЭ)", west("thenationalnews.com")),
    ("Al-Monitor", west("al-monitor.com")),
    ("Радио Свобода / RFE/RL", west("rferl.org")),
    *WORLD,

    # НЕ берём (раскомментируй, если решите иначе):
    # ("Al Jazeera", ...)          — шейх: много лжи, антисаудовские
    # ("Al-Araby", ...)            — катарский, антисаудовский
    # ("Saba.ye", ...)             — агентство хуситов
    # ("Ad-Diyar", ...)            — близок к «Хизбалле»
    # ("ANF", "JINHA", ...)        — СМИ РПК
    # ("dailyislamist", "tg:dailyislamist")  — турецкий исламистский канал (ихвановский уклон)
    # ("Ислам сегодня", "Sputnik")  — нововведенцы; российские госСМИ (для СНГ берём местные надёжные)
]

# Надёжные источники (по совету шейха: саудовские, официальные агентства Сирии, Ирака, Йемена,
# официальные религиозные органы КСА). Однозначную новость отсюда Claude только переводит,
# новость из остальных источников — сначала перепроверяет.
TRUSTED_SOURCES = {
    "SaudiNews50", "SPA (агентство КСА)", "Sabq", "Twasul", "Okaz", "Al Riyadh", "Asharq Al-Awsat",
    "Asharq News", "Al Arabiya", "Al-Eqtisad", "Independent Arabia",
    "Министерство исламских дел КСА", "Всемирная исламская лига", "Харамайн",
    "SANA (агентство Сирии)", "INA (агентство Ирака)", "Saba (правительство Йемена)", "Al-Masdar Online",
    "UzA (агентство Узбекистана)", "Ховар (агентство Таджикистана)",   # государственные агентства
    "WAM (агентство ОАЭ)", "KUNA (агентство Кувейта)", "QNA (агентство Катара)", "BNA (агентство Бахрейна)",
    "ONA (агентство Омана)", "Petra (агентство Иордании)", "MENA (агентство Египта)", "NNA (агентство Ливана)",
    "MAP (агентство Марокко)", "SUNA (агентство Судана)", "APS (агентство Алжира)",
    "Reuters", "AP", "AFP", "Arab News", "Saudi Gazette",               # мировые агентства и саудовские на английском
}

# Домены, которые отбрасываются всегда (в том числе в общих поисках Google News)
BLOCKED = ["aljazeera", "alaraby.co.uk", "saba.ye", "addiyar", "anf-news", "jinhaagency",
           "islamtimes", "shiawaves", "almayadeen", "almanar", "alalam", "presstv", "almasirah",
           "arabi21", "noonpost", "middleeasteye", "alquds.co.uk", "palinfo", "felesteen", "shehabnews",
           "tasnimnews", "shafaqna", "farsnews", "mehrnews", "irna.ir", "abna24", "alkawthartv",
           "middleeastmonitor", "tehrantimes", "hispantv", "kayhan", "5pillarsuk", "english.almayadeen",
           "qudsnen", "ajplus", "iqna",
           # исламофобские и националистические сайты — о мусульманах пишут недостоверно
           "resistancerepublicaine", "ripostelaique", "fdesouche", "organiser.org", "opindia", "breitbart",
           # нововведенцы и ихвановский уклон
           "islam-today.ru", "islamnews.ru",
           # российские госСМИ (для новостей СНГ берём местные надёжные издания)
           "//ria.ru", "//tass.ru", "//tass.com", "//rt.com", "//www.rt.com", "//russian.rt.com", "//arabic.rt.com",
           "sputnik", "//lenta.ru", "//iz.ru", "//rg.ru", "//www.rg.ru", "//www.kp.ru", "//aif.ru", "//www.aif.ru",
           "//www.vesti.ru", "//smotrim.ru", "//www.1tv.ru", "//www.ntv.ru", "tsargrad", "//regnum.ru",
           "//ren.tv", "eadaily"]

# Арабские издания не из доверенных. Переключатель «Арабский мир — только доверенные» (/sources arab on)
# их не берёт: новости арабского мира тогда — только от саудовских, официальных агентств и других ✓.
ARAB_OTHER = {"Sky News Arabia", "Al Khaleej", "Al Mashhad", "Al Rai (Кувейт)", "SOHR", "Annahar", "Lebanon Debate",
              "Al Markazia", "Sawt Beirut", "Voice of Lebanon", "Youm7", "Shorouk", "El Balad", "Akhbar El Yom",
              "El Aosboa", "Egypt Telegraph", "Jordan Zad", "Madar News", "El Djazair El Djadida", "Enab Baladi (Сирия)",
              "Al-Ahram", "Erem News", "Monte Carlo Doualiya", "Euronews Arabic", "InfoMigrants", "Hormuz Report",
              f"{WORLD_SEARCH} (араб.)"}

# Авторитетные издания мира. Переключатель «Мир — только авторитетные» (/sources world on):
# из общих поисков «🌍 Поиск» берём только их — без жёлтой прессы, блогов и незнакомых сайтов.
REPUTABLE = {
    # мировые агентства и англоязычные
    "reuters.com", "apnews.com", "afp.com", "bbc.com", "bbc.co.uk", "theguardian.com", "nytimes.com",
    "washingtonpost.com", "wsj.com", "ft.com", "bloomberg.com", "economist.com", "politico.com", "politico.eu",
    "axios.com", "cnn.com", "nbcnews.com", "cbsnews.com", "abcnews.go.com", "npr.org", "pbs.org", "latimes.com",
    "independent.co.uk", "telegraph.co.uk", "thetimes.co.uk", "thetimes.com", "news.sky.com", "itv.com",
    "channel4.com", "irishtimes.com", "rte.ie", "cbc.ca", "theglobeandmail.com", "abc.net.au", "smh.com.au",
    "rnz.co.nz", "euronews.com",
    # Франция, Германия, Италия, Испания, Бенилюкс, Швейцария, Австрия, Скандинавия
    "france24.com", "rfi.fr", "lemonde.fr", "lefigaro.fr", "liberation.fr", "francetvinfo.fr", "ouest-france.fr",
    "leparisien.fr", "lexpress.fr", "lepoint.fr", "20minutes.fr", "bfmtv.com", "tf1info.fr", "la-croix.com",
    "spiegel.de", "zeit.de", "faz.net", "sueddeutsche.de", "tagesschau.de", "zdf.de", "dw.com", "welt.de",
    "n-tv.de", "stern.de", "br.de", "ndr.de", "wdr.de", "ansa.it", "corriere.it", "repubblica.it", "lastampa.it",
    "rainews.it", "ilsole24ore.com", "elpais.com", "elmundo.es", "rtve.es", "lavanguardia.com", "nos.nl", "nrc.nl",
    "volkskrant.nl", "rtbf.be", "lesoir.be", "vrt.be", "swissinfo.ch", "nzz.ch", "rts.ch", "srf.ch", "orf.at",
    "derstandard.at", "svt.se", "dn.se", "dr.dk", "nrk.no", "yle.fi",
    # арабские доверенные на английском, Турция
    "arabnews.com", "saudigazette.com.sa", "spa.gov.sa", "aawsat.com", "english.aawsat.com", "alarabiya.net",
    "thenationalnews.com", "gulfnews.com", "khaleejtimes.com", "aa.com.tr", "trtworld.com", "dailysabah.com",
    "hurriyetdailynews.com",
    # Азия и Африка
    "dawn.com", "tribune.com.pk", "geo.tv", "thehindu.com", "indianexpress.com", "scroll.in", "ndtv.com",
    "antaranews.com", "thejakartapost.com", "jakartaglobe.id", "kompas.com", "bernama.com", "thestar.com.my",
    "malaymail.com", "nst.com.my", "straitstimes.com", "channelnewsasia.com", "scmp.com", "dailytrust.com",
    "premiumtimesng.com", "punchng.com", "channelstv.com", "nation.africa", "news24.com",
    # СНГ — местные надёжные (без российских госСМИ); «Радио Свобода» и её службы
    "kun.uz", "gazeta.uz", "daryo.uz", "uza.uz", "podrobno.uz", "asiaplustj.info", "khovar.tj", "tengrinews.kz",
    "informburo.kz", "inform.kz", "kursiv.media", "24.kg", "kaktus.media", "akipress.com", "azattyk.org",
    "ozodi.org", "ozodlik.org", "azattyq.org", "rferl.org", "svoboda.org", "currenttime.tv", "kavkazr.com",
    "idelreal.org", "trend.az", "apa.az", "report.az",
}


def reputable(href):
    host = re.sub(r"^www\.", "", urlparse(href or "").netloc.lower())
    return any(host == d or host.endswith("." + d) for d in REPUTABLE)


PER_FEED = 8                 # сколько самых свежих записей брать из одного источника
PER_FEED_TRUSTED = 15        # доверенным — больше: их новости чаще в ленте
PER_FEED_OVERRIDE = {"SaudiNews50": 20, **{n: 30 for n, _ in WORLD}}   # главным источникам и поиску — больше
DUP_HOURS = 72               # повторы сверяем с тем, что брали за 3 дня
ALT_HOLD_MIN = 20            # чередование: вторая тяжёлая подряд — не раньше чем через 20 мин (если нет добрых)

CHANNEL_LINK = "https://t.me/ilm4_info"   # ссылка «Подписаться» под постом
WATERMARK_TEXT = "@ilm4_info"             # текст водяного знака
# Свой логотип вместо текста: файл watermark.png рядом (лучше с прозрачным фоном).
# Свой шрифт: файл watermark.ttf рядом.

MAX_DRAFTS_PER_COLLECT = 15  # сколько черновиков за один сбор (режим API)
MAX_AGE_HOURS = 6            # новости старше этого не берём
PENDING_TTL_HOURS = 48       # черновик без решения дольше этого — снимается
AUTO_MAX_AGE_HOURS = 3       # автопилот публикует только черновики не старше 3 часов
TZ = timezone(timedelta(hours=5))   # часовой пояс канала (Ташкент)

# Начальные значения — потом меняются командами в группе модерации
DEFAULT_SETTINGS = {
    "night": [23, 7],   # ночь: модераторы спят. Обычная очередь ждёт утра,
                        # выходят только отложенные и (если /auto on) проверенные
    "gap": 0,           # минут между постами, одобренными вручную (0 — сразу)
    "night_gap": 0,     # и ночью
    "wm": True,         # водяной знак по умолчанию
    "auto": True,       # автопилот: сразу публиковать то, что Claude пометил «проверено, сомнений нет»
    "auto_hard": True,  # автопилот и для тяжёлых (False — тяжёлые всегда ждут ✅ модератора)
    "auto_top": False,  # режим «самое важное»: сам выпускает только важность 4–5, не больше per_hour в час
    "per_hour": 3,      # сколько самых важных в час в режиме «самое важное»
    # Доступ: кто может управлять ботом. Владелец (ADMIN_IDS) может всегда.
    "access": "list",   # "list" — только владелец и вписанные; "group" — все участники группы «Модер»
    "allow_ids": [],    # вписанные по ID
    "allow_names": [],  # вписанные по нику (без @, маленькими буквами)
    "private_on": False,  # можно работать с ботом в личке (вписанные и владелец)
    "drafts_to": "group",  # куда приходят черновики: "group" — в «Модер», "private" — владельцу в личку
    "private_chat": None,  # чей личный чат получает черновики (по умолчанию — владелец)
    "paused": False,    # пауза всех публикаций
    "flow": "all",      # что присылать на модерацию: "all" — всё; "normal" — без малоценных (важность 3–5);
                        # "top" — только самое важное (важность 4–5);
                        # "elite" — только самое-самое: главное для мусульман, политика, разбор фейков,
                        # разоблачения сект и группировок от доверенных (важность 5 или пометка elite)
    "alternate": True,  # чередовать тяжёлые и добрые в канале (по журналу опубликованного)
    "arab_trusted": True,     # арабский мир — только из доверенных (саудовские, официальные агентства)
    "world_reputable": True,  # из общих поисков по миру — только авторитетные издания
}
FLOW_MIN = {"all": 1, "normal": 3, "top": 4, "elite": 5}
FLOW_NAMES = {"all": "всё", "normal": "без малоценных", "top": "только самое важное",
              "elite": "только самое-самое важное"}


def flow_pass(flow, e):
    """Проходит ли черновик в модерацию при этом режиме потока."""
    if flow == "elite":
        return bool(e.get("elite")) or e.get("importance", 3) >= 5
    return e.get("importance", 3) >= FLOW_MIN.get(flow, 1)

# Только для режима API
MODEL = "claude-sonnet-5-5"  # Sonnet 5.5; мощнее: "claude-opus-5-5", дешевле: "claude-haiku-4-5"
COLLECT_EVERY_MIN = 60

LEASE_BRANCH = "pc-lease"       # ветка-отметка «бот сейчас работает на ПК»
LEASE_SECONDS = 180             # ПК считается выключенным, если отметка старше 3 минут
INBOX_BRANCH = "claude/inbox"   # ветка, куда облачный Claude кладёт черновики
INBOX_DIR = "inbox-branch"      # её копия у редактора

# =============================================================


def load_env():
    """Читает секреты из файла .env (для запуска на своём компьютере)."""
    if os.path.exists(".env"):
        for line in open(".env", encoding="utf-8"):
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


load_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
MOD_CHAT_ID = os.environ.get("MOD_CHAT_ID", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DRY_RUN = os.environ.get("DRY_RUN") == "1"          # ничего не отправлять, только печатать
INBOX_LOCAL = os.environ.get("INBOX_LOCAL") == "1"  # черновики в папке, без git (для проверки)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; IslamNewsBot/1.0)"}
NOW = time.time()


# ---------------------- файлы и время ----------------------

_io_lock = threading.RLock()   # панель читает и пишет файлы из разных потоков — по очереди


def read_json(path, default):
    with _io_lock:
        return _read_json(path, default)


def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:   # файл испорчен (например, выключили ПК) — откладываем в сторону, не падаем
            os.replace(path, path + ".broken")
            print(f"⚠️ Файл {path} был испорчен — отложил в {path}.broken, беру данные заново")
    return default


def write_json(path, data):
    """Запись «всё или ничего»: сначала во временный файл, потом мгновенная замена.
    Если ПК выключат посередине — останется старый целый файл, а не обрывок."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _io_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(30):   # Windows: файл на миг занят (антивирус, поиск, другой поток) — ждём
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.1)
        with open(path, "w", encoding="utf-8") as f:   # за 3 с не освободился — пишем напрямую
            json.dump(data, f, ensure_ascii=False, indent=1)
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------- рабочие файлы бота — в отдельной ветке без истории ----------
# Раньше состояние коммитилось в main каждые 5–15 минут, и репозиторий рос на 3–4 МБ в день.
# Теперь оно лежит в ветке bot-state одним коммитом, который каждый раз перезаписывается.
STATE_BRANCH = "bot-state"
STATE_FILES = ["state.json", "triage_seen.json", "shortlist.json", "pool.json"]
STATE_REF = f"refs/remotes/origin/{STATE_BRANCH}"


def state_pull():
    """Скачивает рабочие файлы из ветки bot-state. Ветки ещё нет — остаются файлы из main."""
    if DRY_RUN or INBOX_LOCAL:
        return False
    if git("fetch", "-q", "--depth=1", "origin", f"+{STATE_BRANCH}:{STATE_REF}").returncode != 0:
        return False
    for name in STATE_FILES:
        r = subprocess.run(["git", "show", f"{STATE_REF}:{name}"], capture_output=True)
        if r.returncode == 0:
            try:
                write_json(name, json.loads(r.stdout.decode("utf-8")))
            except ValueError:
                print("⚠️ В ветке bot-state испорчен", name)
    return True


def state_push(message="Состояние бота"):
    """Отправляет рабочие файлы в bot-state одним коммитом без истории (перезаписью)."""
    if DRY_RUN or INBOX_LOCAL:
        return True
    rows = []
    for name in STATE_FILES:
        if os.path.exists(name):
            blob = git("hash-object", "-w", name).stdout.strip()
            rows.append(f"100644 blob {blob}\t{name}\0")
    tree = subprocess.run(["git", "mktree", "-z"], input="".join(rows), capture_output=True,
                          text=True).stdout.strip()
    commit = git("-c", "user.name=news-bot", "-c", "user.email=news-bot@users.noreply.github.com",
                 "commit-tree", tree, "-m", message).stdout.strip()
    ok = bool(commit) and git("push", "-q", "-f", "origin", f"{commit}:refs/heads/{STATE_BRANCH}").returncode == 0
    if not ok:
        print("⚠️ Состояние не отправилось на GitHub (нет интернета?) — попробую в следующий раз")
    return ok


def load_state():
    state = read_json("state.json", {})
    for key, default in (("offset", 0), ("drafts", {}), ("queue", {}), ("wm", {}),
                         ("last_publish", 0), ("last_tone", ""), ("last_collect", 0),
                         ("inbox_done", []), ("published_log", []), ("dupes", []), ("held", [])):
        state.setdefault(key, default)
    state["settings"] = {**DEFAULT_SETTINGS, **state.get("settings", {})}
    return state


def save_state(state):
    state["wm"] = dict(list(state["wm"].items())[-300:])
    state["inbox_done"] = state["inbox_done"][-500:]
    for key in ("published_log", "dupes", "held"):   # журналы — за 3 дня
        state[key] = [x for x in state[key] if NOW - x.get("t", 0) < DUP_HOURS * 3600][-500:]
    write_json("state.json", state)


def local_now():
    return datetime.fromtimestamp(NOW, TZ)


def fmt(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def next_time(hour, minute=0, days=0):
    """Ближайшие hour:minute по Ташкенту (сегодня или завтра) + days дней."""
    t = local_now().replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days)
    if days == 0 and t.timestamp() <= NOW:
        t += timedelta(days=1)
    return t.timestamp()


def is_night(settings):
    n = settings.get("night")
    if not n:
        return False
    start, end, h = n[0], n[1], local_now().hour
    return start <= h < end if start < end else (h >= start or h < end)


def pc_is_running():
    """Есть ли свежая отметка от бота на ПК."""
    if git("fetch", "-q", "--depth=1", "origin", LEASE_BRANCH).returncode != 0:
        return False
    try:
        beat = json.loads(git("show", "FETCH_HEAD:lease.json").stdout)["heartbeat"]
    except Exception:
        return False
    return time.time() - beat < LEASE_SECONDS


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")


# ---------------------- Telegram ----------------------

_fake_id = [1000]


def tg(method, files=None, quiet=False, **params):
    """Вызов Telegram Bot API. Возвращает result или None при ошибке."""
    if DRY_RUN:
        if method.startswith("send") or method == "copyMessage":
            text = params.get("text") or params.get("caption") or ""
            media = params.get("photo") or params.get("video") or ""
            print(f"\n----- [{method}] {media if isinstance(media, str) else '(файл)'}\n{text}\n-----")
        _fake_id[0] += 1
        return [] if method == "getUpdates" else {"message_id": _fake_id[0]}

    data = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
            for k, v in params.items() if v is not None}
    for _ in range(3):
        try:
            j = requests.post(f"{API}/{method}", data=data, files=files, timeout=120).json()
        except Exception as e:
            print(f"Telegram {method}: сеть — {e}")
            return None
        if j.get("ok"):
            return j["result"]
        retry = j.get("parameters", {}).get("retry_after")
        if retry:
            time.sleep(retry + 1)
            continue
        if not quiet:
            print(f"Telegram {method}: {j.get('description')}")
        return None
    return None


def download(url, limit_mb):
    try:
        with requests.get(url, headers=UA, timeout=90, stream=True) as r:
            r.raise_for_status()
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > limit_mb * 1024 * 1024:
                    return None
            return data
    except Exception as e:
        print("Не скачалось:", url[:100], e)
        return None


PRIVATE_BASE = 1_000_000     # черновик в личке: номер = 1 000 000 + номер сообщения (не путается с группой)
CHAT = {"reply": None, "work": None}   # reply — откуда пришла команда; work — куда идут черновики


def work_chat(state):
    """Куда отправлять черновики и сообщения бота: группа «Модер» или личка владельца."""
    s = state["settings"]
    if s.get("drafts_to") == "private" and s.get("private_on"):
        return s.get("private_chat") or (min(ADMIN_IDS) if ADMIN_IDS else MOD_CHAT_ID)
    return MOD_CHAT_ID


def key_of(chat_id, message_id):
    """Номер черновика: в группе — номер сообщения, в личке — 1 000 000 + номер."""
    return str(message_id) if str(chat_id) == str(MOD_CHAT_ID) else str(PRIVATE_BASE + int(message_id))


def where(state, mid):
    """(чат, номер сообщения) черновика."""
    d = state["drafts"].get(mid, {})
    return d.get("chat", MOD_CHAT_ID), d.get("msg_id", int(mid))


def say(text, reply_to=None):
    tg("sendMessage", chat_id=CHAT["reply"] or CHAT["work"] or MOD_CHAT_ID, text=text, parse_mode="HTML",
       reply_parameters={"message_id": reply_to} if reply_to else None)


# ---------------------- кнопки черновика ----------------------

def keyboard(d, status, wm=False, at=None):
    """d — запись черновика: tone, safe, note, url, media."""
    tone = {"good": "🟢 добрая", "hard": "🔴 тяжёлая"}.get(d.get("tone"), "⚪")
    rows = {
        "pending": [[{"text": "✅ Опубликовать", "callback_data": "ok"},
                     {"text": "❌ Отклонить", "callback_data": "no"}],
                    [{"text": "🕒 Отложить", "callback_data": "later"}]],
        "later": [[{"text": "🌙 Ночью в 02:00", "callback_data": "at:n"},
                   {"text": "🌅 Утром в 08:00", "callback_data": "at:m"}],
                  [{"text": "⏱ Через 3 часа", "callback_data": "at:3"},
                   {"text": "📅 Завтра в 12:00", "callback_data": "at:t"}],
                  [{"text": "✍️ Своё время: ответь /at 21:30", "callback_data": "-"}],
                  [{"text": "↩️ Назад", "callback_data": "back"}]],
        "queued": [[{"text": "⏳ В очереди на публикацию", "callback_data": "-"}],
                   [{"text": "🕒 Отложить", "callback_data": "later"},
                    {"text": "↩️ Отменить", "callback_data": "no"}]],
        "auto": [[{"text": "🤖 Проверено — выйдет автоматически", "callback_data": "-"}],
                 [{"text": "↩️ Отменить", "callback_data": "no"}]],
        "scheduled": [[{"text": f"🕒 Выйдет {fmt(at) if at else ''}", "callback_data": "-"}],
                      [{"text": "🕒 Другое время", "callback_data": "later"},
                       {"text": "↩️ Отменить", "callback_data": "no"}]],
        "rejected": [[{"text": "❌ Отклонено · вернуть?", "callback_data": "ok"}]],
        "expired": [[{"text": "⌛ Срок вышел · вернуть?", "callback_data": "ok"}]],
        "published": [[{"text": "📢 Опубликовано", "callback_data": "-"}]],
    }[status]
    if d.get("media") and status in ("pending", "queued", "auto", "scheduled"):
        rows.append([{"text": f"💧 Водяной знак: {'да' if wm else 'нет'}", "callback_data": "wm"}])
    if status == "pending" and "safe" in d:
        if d["safe"] and d.get("trusted"):
            verdict = "🤖 Доверенный источник, проверено"
        elif d["safe"]:
            verdict = "🔎 Claude сверил, но источник не из доверенных"
        else:
            verdict = f"⚠️ {d.get('note') or 'Нужна проверка'}"
        rows.append([{"text": verdict, "callback_data": "-"}])
    num = f" · №{d['n']}" if d.get("n") else ""   # номер для /пачка и «Бот, выложи 53…»
    if d.get("url"):
        rows.append([{"text": f"🔗 Оригинал · {tone}{num}", "url": d["url"]}])
    elif num:
        rows.append([{"text": f"{tone}{num}", "callback_data": "-"}])
    return {"inline_keyboard": rows}


def markup_url(msg):
    for row in (msg.get("reply_markup") or {}).get("inline_keyboard", []):
        for b in row:
            if b.get("url"):
                return b["url"]
    return None


def snapshot(msg):
    """Всё, что нужно, чтобы потом опубликовать черновик (в т.ч. с водяным знаком)."""
    snap = {"kind": "text", "text": msg.get("text") or msg.get("caption") or "",
            "entities": msg.get("entities") or msg.get("caption_entities") or []}
    if msg.get("video"):
        v = msg["video"]
        snap.update(kind="video", file_id=v["file_id"], width=v.get("width"), height=v.get("height"))
    elif msg.get("photo"):
        snap.update(kind="photo", file_id=msg["photo"][-1]["file_id"])
    return snap


def status_of(state, mid):
    q = state["queue"].get(mid)
    if q:
        return "scheduled" if q.get("at") else "auto" if q.get("auto") else "queued"
    return state["drafts"].get(mid, {}).get("status", "pending")


def refresh(state, mid, status=None):
    d = state["drafts"].get(mid, {})
    status = status or status_of(state, mid)
    wm = state["wm"].get(mid, state["settings"]["wm"])
    at = state["queue"].get(mid, {}).get("at")
    chat, msg_id = where(state, mid)
    tg("editMessageReplyMarkup", chat_id=chat, message_id=msg_id,
       reply_markup=keyboard(d, status, wm, at), quiet=True)


# ---------------------- дежурный: модерация ----------------------

def process_updates(state, wait=0):
    """wait > 0 — ждать новых нажатий до wait секунд (бот на ПК отвечает мгновенно)."""
    register_commands(state)
    CHAT["work"] = work_chat(state)
    updates = tg("getUpdates", offset=state["offset"], timeout=wait,
                 allowed_updates=["callback_query", "message"]) or []
    for u in updates:
        state["offset"] = u["update_id"] + 1
        CHAT["reply"] = ((u.get("message") or (u.get("callback_query") or {}).get("message") or {})
                         .get("chat", {}).get("id"))
        try:
            if "callback_query" in u:
                handle_button(state, u["callback_query"])
            elif "message" in u:
                handle_message(state, u["message"])
        except Exception as e:
            print("Не смог обработать обновление:", repr(e))
        finally:
            CHAT["reply"] = None
            CHAT["work"] = work_chat(state)


def listed(state, user):
    """Владелец или вписанный (по ID или нику)."""
    s = state["settings"]
    name = (user.get("username") or "").lower()
    return (user.get("id") in ADMIN_IDS or user.get("id") in s.get("allow_ids", [])
            or bool(name and name in s.get("allow_names", [])))


def allowed(state, chat, user, sender_chat=None):
    """Кого бот слушает: в личке — вписанных (если личка включена), в группе — по режиму доступа."""
    s = state["settings"]
    if chat.get("type") == "private":   # владелец — всегда (чтобы мог включить личку), вписанные — если включена
        return user.get("id") in ADMIN_IDS or (bool(s.get("private_on")) and listed(state, user))
    if str(chat.get("id")) != str(MOD_CHAT_ID):
        return False
    if sender_chat and str(sender_chat.get("id")) == str(MOD_CHAT_ID):
        return True   # анонимный админ группы пишет от имени группы
    return not ADMIN_IDS or listed(state, user) or s.get("access") == "group"


def is_owner(user, sender_chat=None):
    """Доступом управляет только владелец (ADMIN_IDS) или анонимный админ группы."""
    if sender_chat and str(sender_chat.get("id")) == str(MOD_CHAT_ID):
        return True
    return not ADMIN_IDS or user.get("id") in ADMIN_IDS


def ensure_draft(state, msg):
    """Запись о черновике (если бот её не знает — создаём по сообщению)."""
    chat = msg.get("chat", {}).get("id", MOD_CHAT_ID)
    mid = key_of(chat, msg["message_id"])
    if mid not in state["drafts"]:
        state["drafts"][mid] = {"tone": "", "url": markup_url(msg), "created": NOW, "chat": chat,
                                "msg_id": msg["message_id"],
                                "media": bool(msg.get("photo") or msg.get("video")), "status": "pending"}
    return mid


def enqueue(state, mid, msg, **extra):
    d = state["drafts"][mid]
    state["queue"][mid] = {"tone": d.get("tone", ""), "approved_at": NOW, "msg": snapshot(msg), **extra}
    d["status"] = "queued"


def handle_button(state, cq):
    msg = cq.get("message") or {}
    if not allowed(state, msg.get("chat", {}), cq.get("from", {})):
        return
    tg("answerCallbackQuery", callback_query_id=cq["id"], quiet=True)   # старое нажатие — не страшно
    data = cq.get("data", "")
    if data == "-" or "message_id" not in msg:
        return
    if data.startswith("m:"):
        return menu_button(state, msg, data[2:], owner=is_owner(cq.get("from", {})))
    mid = ensure_draft(state, msg)
    d = state["drafts"][mid]
    if d.get("status") == "published":
        return

    print(f"Кнопка «{data}» под черновиком {mid}")
    if data == "ok":
        enqueue(state, mid, msg)
    elif data == "no":
        state["queue"].pop(mid, None)
        d["status"] = "rejected"
    elif data == "wm":
        state["wm"][mid] = not state["wm"].get(mid, state["settings"]["wm"])
    elif data == "later":
        return refresh(state, mid, "later")
    elif data.startswith("at:"):
        at = {"n": lambda: next_time(2), "m": lambda: next_time(8),
              "3": lambda: NOW + 3 * 3600, "t": lambda: next_time(12, days=1)}[data[3:]]()
        enqueue(state, mid, msg, at=at)
    # "back" — просто вернуть кнопки
    refresh(state, mid)


def parse_at(args):
    """«21:30», «21», «27.09 21:30» -> время (Ташкент) или None."""
    text = " ".join(args)
    m = re.fullmatch(r"(?:(\d{1,2})\.(\d{1,2})\s+)?(\d{1,2})(?::(\d{2}))?", text.strip())
    if not m:
        return None
    day, month, hour, minute = m.group(1), m.group(2), int(m.group(3)), int(m.group(4) or 0)
    if hour > 23 or minute > 59:
        return None
    if day:
        now = local_now()
        try:
            t = now.replace(month=int(month), day=int(day), hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if t < now:
            t = t.replace(year=t.year + 1)
        return t.timestamp()
    return next_time(hour, minute)


def handle_message(state, msg):
    """Обычные сообщения и ответы в группе — просто разговор, бот их не трогает.
    Бот реагирует только на команды: /at и /edit (ответом на черновик) и /status, /night …"""
    if not allowed(state, msg.get("chat", {}), msg.get("from", {}), msg.get("sender_chat")):
        return
    CHAT["reply"] = msg["chat"]["id"]      # отвечаем туда, откуда написали (группа или личка)
    text = msg.get("text") or ""
    private = msg.get("chat", {}).get("type") == "private"
    if BOT_CALL.search(text) or (private and text and not text.startswith("/")):
        return assistant(state, msg)   # в личке любое обычное сообщение — помощнику
    if not text.startswith("/"):
        return
    first = text.split()[0].split("@")[0].lower()
    orig = msg.get("reply_to_message") or {}
    mid = key_of(msg["chat"]["id"], orig["message_id"]) if orig.get("message_id") else ""
    is_draft = mid in state["drafts"] and status_of(state, mid) not in ("published",)

    if first in ("/at", "/edit"):
        if not is_draft:
            return say(f"Команду {first} нужно отправить <b>ответом на черновик</b> новости.", msg["message_id"])
        if first == "/edit":
            return handle_edit(state, msg, orig)
        at = parse_at(text.split()[1:])
        if not at:
            return say("Пример: <code>/at 21:30</code> или <code>/at 27.09 21:30</code>", msg["message_id"])
        enqueue(state, mid, orig, at=at)
        refresh(state, mid)
        return say(f"🕒 Выйдет {fmt(at)}", msg["message_id"])
    handle_command(state, text, owner=is_owner(msg.get("from", {}), msg.get("sender_chat")),
                   who=msg.get("from", {}), reply=orig)


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def handle_edit(state, msg, orig):
    """«/edit новый текст» ответом на черновик = замена текста (форматирование сохраняется)."""
    text = msg["text"]
    m = re.match(r"/edit(?:@\w+)?\s*", text)
    body = text[m.end():]
    if not body.strip():
        return say("Напиши новый текст после команды: <code>/edit Новый текст…</code>", msg["message_id"])
    shift = utf16_len(text[:m.end()])
    entities = []
    for e in msg.get("entities", []):   # сдвигаем жирный, ссылки и т.п. на длину «/edit »
        start, end = max(e["offset"], shift), e["offset"] + e["length"]
        if end > start and e.get("type") != "bot_command":
            entities.append({**e, "offset": start - shift, "length": end - start})

    base = {"chat_id": msg["chat"]["id"], "message_id": orig["message_id"],
            "reply_markup": orig.get("reply_markup")}   # без этого кнопки пропадут
    if orig.get("text") is not None:
        res = tg("editMessageText", text=body, entities=entities, **base)
    else:
        res = tg("editMessageCaption", caption=body, caption_entities=entities, **base)
    mid = key_of(msg["chat"]["id"], orig["message_id"])
    if res and mid in state["queue"]:
        state["queue"][mid]["msg"] = snapshot(res)
    if res and mid in state["drafts"]:
        state["drafts"][mid]["snap"] = snapshot(res)
    say("✏️ Текст черновика обновлён." if res else
        "⚠️ Не получилось заменить текст (у поста с фото/видео лимит 1024 символа).", msg["message_id"])


HELP = """<b>Команды бота</b>
/menu — панель управления кнопками
/status — очередь и настройки
/night 23 7 — ночь с 23:00 до 7:00 (модераторы спят) · /night off
/auto on — проверенное Claude публикуется сразу (и добрые, и тяжёлые)
/auto good — сразу только добрые, тяжёлые ждут твоей ✅ · /auto off — сам ничего не публикует
/auto top 3 — сам выпускает только САМОЕ ВАЖНОЕ, до 3 в час; остальное приходит тебе, как обычно
/pause — остановить вообще все публикации (и очередь, и отложенные) · /resume
/интервал 0 — всё проверенное и одобренное ✅ выходит сразу (по умолчанию)
/интервал 15 — выходит по одной раз в 15 минут (тоже /gap 15)
/пачка 15 — то, что уже ждёт в очереди, выпустить по одной: первую сразу, дальше каждые 15 мин
/пачка 10 53 55 58 — эти черновики выпустить по одному раз в 10 мин (№ — на кнопке «🔗 Оригинал»)
/пачка 10 все — все черновики, что ждут решения, по одному раз в 10 мин
/wm on · /wm off — водяной знак по умолчанию

<b>Поток и источники</b>
/flow all — присылать всё · /flow normal — без малоценных · /flow top — только самое важное (4–5 из 5)
/flow elite — только самое-самое: главное для мусульман, большая политика, разбор фейков, разоблачения сект
/alt on · /alt off — чередовать тяжёлые и добрые в канале (по журналу опубликованного)
/sources arab on|off — арабский мир только из доверенных (саудовские, официальные агентства)
/sources world on|off — по миру только авторитетные издания (без жёлтой прессы)
Повторы за 3 дня бот отсеивает сам: не присылает и не публикует.

<b>Доступ</b> (меняет только владелец)
/users — кто может управлять ботом
/allow 123456789 · /allow @nickname · или ответом на сообщение человека — добавить
/deny … — убрать · /access list — слушать только список · /access group — всю группу «Модер»
/private on — работать с ботом в личке · /drafts private — черновики в личку · /drafts group — в группу

<b>Под черновиком</b>: ✅ в очередь, ❌ отклонить, 🕒 отложить, 💧 водяной знак.
Своё время: ответь на черновик <code>/at 21:30</code> или <code>/at 27.09 21:30</code>.
Исправить текст: ответь на черновик <code>/edit Новый текст…</code>
Обычные сообщения и ответы бот не трогает — можно спокойно переписываться.

Ночью очередь, одобренная вручную, ждёт утра; выходят отложенные и (с /auto on) проверенные Claude.
Когда включён ПК — бот отвечает сразу; без ПК бот просыпается раз в ~5 минут.
Можно и словами: «Бот, выложи 53, 55 и 58 раз в 10 минут»."""


def handle_command(state, text, quiet=False, owner=True, who=None, reply=None):
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    s = state["settings"]
    arg = args[0].lower() if args else ""

    if cmd in ACCESS_COMMANDS:
        if not owner:
            return say("🔒 Доступом управляет только владелец бота.")
        note = access_command(state, cmd, args, who or {}, reply or {})
        CHAT["work"] = work_chat(state)
        if not quiet or note.startswith("⚠️"):
            say(note)
        return

    if cmd in ("/start", "/help"):
        return say(HELP)
    if cmd in ("/menu", "/меню", "/panel"):
        return send_menu(state)
    if cmd in ("/night", "/quiet"):
        if arg in ("off", "выкл"):
            s["night"] = None
        elif len(args) == 2 and all(a.isdigit() and 0 <= int(a) <= 23 for a in args):
            s["night"] = [int(args[0]), int(args[1])]
        else:
            return say("Пример: <code>/night 23 7</code> или <code>/night off</code>")
    elif cmd in ("/gap", "/nightgap", "/интервал", "/interval"):
        if not arg.isdigit():
            return say(f"Пример: <code>{cmd} 15</code> (или <code>{cmd} 0</code> — сразу)")
        s["night_gap" if cmd == "/nightgap" else "gap"] = int(arg)
    elif cmd in ("/пачка", "/batch"):
        if args[1:2] and args[1].lower() in ("все", "всё", "all"):
            args = [arg] + [m for m, d in state["drafts"].items()
                            if status_of(state, m) == "pending" and d.get("status") != "expired"]
        if not arg.isdigit() or not all(a.isdigit() for a in args):
            return say("Пример: <code>/пачка 15</code> — очередь по одной раз в 15 мин;\n"
                       "<code>/пачка 10 53 55 58</code> — эти черновики раз в 10 мин (№ — на кнопке под черновиком);\n"
                       "<code>/пачка 10 все</code> — все черновики, что ждут решения")
        return say(spread(state, int(arg), args[1:]))
    elif cmd in ("/auto", "/автопилот"):
        modes = {"on": (True, True, False), "вкл": (True, True, False), "off": (False, False, False),
                 "выкл": (False, False, False), "good": (True, False, False), "добрые": (True, False, False),
                 "top": (True, True, True), "важное": (True, True, True)}
        if arg not in modes:
            return say("Пример: <code>/auto on</code> — все проверенные сразу; "
                       "<code>/auto good</code> — сразу только добрые, тяжёлые ждут ✅; "
                       "<code>/auto top 3</code> — сам выпускает только самое важное, до 3 в час; "
                       "<code>/auto off</code> — ничего сам не публикует")
        s["auto"], s["auto_hard"], s["auto_top"] = modes[arg]
        if s["auto_top"] and args[1:2] and args[1].isdigit():
            s["per_hour"] = max(1, min(12, int(args[1])))
        note = stop_auto(state)
        if not quiet or note.startswith("↩️"):
            say(note)
    elif cmd in ("/flow", "/поток"):
        names = {"all": "all", "все": "all", "всё": "all", "normal": "normal", "обычный": "normal",
                 "top": "top", "важное": "top", "важные": "top",
                 "elite": "elite", "главное": "elite", "самоесамое": "elite", "самое-самое": "elite"}
        if arg not in names:
            return say("Что присылать на модерацию: <code>/flow all</code> — всё; <code>/flow normal</code> — "
                       "без малоценных; <code>/flow top</code> — только самое важное (4–5 из 5); "
                       "<code>/flow elite</code> — только самое-самое: главное для мусульман, большая политика, "
                       "разбор фейков, разоблачения сект и группировок")
        s["flow"] = names[arg]
    elif cmd in ("/alt", "/чередование"):
        if arg not in ("on", "off"):
            return say("<code>/alt on</code> — чередовать тяжёлые и добрые в канале; <code>/alt off</code>")
        s["alternate"] = arg == "on"
    elif cmd in ("/sources", "/источники"):
        key = {"arab": "arab_trusted", "араб": "arab_trusted", "world": "world_reputable", "мир": "world_reputable"}
        val = args[1].lower() if len(args) > 1 else ""
        if arg not in key or val not in ("on", "off"):
            return say("<code>/sources arab on</code> — арабский мир только из доверенных (саудовские, "
                       "официальные агентства); <code>/sources world on</code> — по миру только авторитетные "
                       "издания; <code>off</code> — брать шире")
        s[key[arg]] = val == "on"
    elif cmd == "/wm":
        if arg not in ("on", "off"):
            return say(f"Пример: <code>{cmd} on</code> или <code>{cmd} off</code>")
        s[cmd[1:]] = arg == "on"
    elif cmd == "/pause":
        s["paused"] = True
    elif cmd == "/resume":
        s["paused"] = False
    elif cmd != "/status":
        return
    if not quiet:
        say(status_text(state))


def spread(state, minutes, ids=()):
    """Выпустить по одной с интервалом: первую сразу, дальше каждые `minutes` минут (с чередованием тона).
    ids — номера черновиков (модератор сам их выбрал); без ids — всё, что уже ждёт в очереди."""
    items, skipped = [], []
    if ids:
        for i in ids:
            mid, d = str(i), state["drafts"].get(str(i))
            snap = (state["queue"].get(mid) or {}).get("msg") or (d or {}).get("snap")
            if not d or d.get("status") in ("published",) or not snap:
                skipped.append(str(i))
                continue
            items.append((mid, {"tone": d.get("tone", ""), "approved_at": NOW, "msg": snap}))
    else:
        items = [(m, q) for m, q in sorted(state["queue"].items(), key=lambda kv: kv[1]["approved_at"])
                 if not q.get("at")]
    if not items:
        return "Нечего распределять: очередь пуста" + (f" (не нашёл черновики {', '.join(skipped)})" if skipped else "") + \
            ".\nЧтобы выпустить черновики из группы: <code>/пачка 10 53 55 58</code> (№ — на кнопке под черновиком)\nили <code>/пачка 10 все</code> — все, что ждут решения."
    lines = []
    for n, (mid, item) in enumerate(alternate(items, state["last_tone"])):
        item = {**item, "at": NOW + n * minutes * 60}
        item.pop("auto", None)
        state["queue"][mid] = item
        state["drafts"][mid]["status"] = "queued"
        refresh(state, mid)
        lines.append(f"{'🟢' if item['tone'] != 'hard' else '🔴'} {fmt(item['at'])} — {draft_title(state['drafts'][mid])[:60]}")
    return (f"🕒 Выпущу по одной раз в {minutes} мин:\n" + "\n".join(lines) +
            (f"\n\nНе нашёл: {', '.join(skipped)}" if skipped else ""))


def menu_markup(state, ask=None):
    """Панель управления кнопками. ask — команда, которую надо подтвердить (выпуск черновиков)."""
    s = state["settings"]
    mark = lambda on, text: ("• " + text + " •") if on else text
    if ask:
        n = sum(status_of(state, m) == "pending" and d.get("status") != "expired"
                for m, d in state["drafts"].items())
        mins = ask.split()[1]
        return {"inline_keyboard": [
            [{"text": f"Выпустить все {n} ждущих черновиков, раз в {mins} мин?", "callback_data": "-"}],
            [{"text": "✅ Да, выпускай", "callback_data": "m:" + ask},
             {"text": "↩️ Нет", "callback_data": "m:refresh"}]]}
    auto = "off" if not s["auto"] else "top" if s.get("auto_top") else "on" if s.get("auto_hard", True) else "good"
    per = s.get("per_hour", 3)
    top_row = [[{"text": "Самое важное — сколько в час:", "callback_data": "-"}],
               [{"text": mark(per == n, str(n)), "callback_data": f"m:/auto top {n}"} for n in (1, 2, 3, 4, 6)]] \
        if auto == "top" else []
    return {"inline_keyboard": [
        [{"text": "🤖 Автопубликация проверенных:", "callback_data": "-"}],
        [{"text": mark(auto == "on", "Все"), "callback_data": "m:/auto on"},
         {"text": mark(auto == "good", "Только добрые"), "callback_data": "m:/auto good"}],
        [{"text": mark(auto == "top", "Самое важное"), "callback_data": f"m:/auto top {per}"},
         {"text": mark(auto == "off", "Выкл"), "callback_data": "m:/auto off"}],
        *top_row,
        [{"text": "📥 Что присылать на модерацию:", "callback_data": "-"}],
        [{"text": mark(s.get("flow") == f, n), "callback_data": f"m:/flow {f}"}
         for f, n in (("all", "Всё"), ("normal", "Без малоценных"))],
        [{"text": mark(s.get("flow") == f, n), "callback_data": f"m:/flow {f}"}
         for f, n in (("top", "Самое важное"), ("elite", "Самое-самое"))],
        [{"text": "🔀 Чередовать тяжёлые и добрые: " + ("вкл" if s.get("alternate", True) else "выкл"),
          "callback_data": "m:/alt " + ("off" if s.get("alternate", True) else "on")}],
        [{"text": "🕌 Арабский мир: " + ("только доверенные" if s.get("arab_trusted") else "все издания"),
          "callback_data": "m:/sources arab " + ("off" if s.get("arab_trusted") else "on")}],
        [{"text": "🌍 Мир: " + ("только авторитетные" if s.get("world_reputable") else "все издания"),
          "callback_data": "m:/sources world " + ("off" if s.get("world_reputable") else "on")}],
        [{"text": "⏱ Интервал между постами:", "callback_data": "-"}],
        [{"text": mark(s["gap"] == g, "сразу" if not g else f"{g} мин"), "callback_data": f"m:/gap {g}"}
         for g in (0, 15, 30, 60)],
        [{"text": "📦 Выпустить ждущие черновики по одной:", "callback_data": "-"}],
        [{"text": f"раз в {g} мин", "callback_data": f"m:ask:/batch {g} all"} for g in (10, 15, 30)],
        [{"text": "💧 Водяной знак: " + ("вкл" if s["wm"] else "выкл"),
          "callback_data": "m:/wm " + ("off" if s["wm"] else "on")},
         {"text": "🌙 Ночь: " + (f"{s['night'][0]}–{s['night'][1]}" if s.get("night") else "выкл"),
          "callback_data": "m:/night " + ("off" if s.get("night") else "23 7")}],
        [{"text": "▶️ Продолжить публикации" if s["paused"] else "⏸ Пауза: остановить всё",
          "callback_data": "m:/resume" if s["paused"] else "m:/pause"}],
        [{"text": "🔐 Кого слушать: " + ("вся группа" if s.get("access") == "group" else "только список"),
          "callback_data": "m:/access " + ("list" if s.get("access") == "group" else "group")}],
        [{"text": "💬 Личка: " + ("вкл" if s.get("private_on") else "выкл"),
          "callback_data": "m:/private " + ("off" if s.get("private_on") else "on")},
         {"text": "📥 Черновики: " + ("в личку" if s.get("drafts_to") == "private" and s.get("private_on") else "в группу"),
          "callback_data": "m:/drafts " + ("group" if s.get("drafts_to") == "private" else "private")}],
        [{"text": "🔄 Обновить", "callback_data": "m:refresh"},
         {"text": "📖 Все команды", "callback_data": "m:/help"}],
    ]}


def send_menu(state):
    tg("sendMessage", chat_id=CHAT["reply"] or CHAT["work"] or MOD_CHAT_ID, text=status_text(state),
       parse_mode="HTML", reply_markup=menu_markup(state))


def menu_button(state, msg, data, owner=True):
    """Нажатие на панели: выполняем команду и обновляем саму панель (без лишних сообщений)."""
    print(f"Панель: «{data}»")
    chat = msg.get("chat", {}).get("id", MOD_CHAT_ID)
    if data.startswith("ask:"):
        return tg("editMessageReplyMarkup", chat_id=chat, message_id=msg["message_id"],
                  reply_markup=menu_markup(state, ask=data[4:]), quiet=True)
    if data != "refresh":
        handle_command(state, data, quiet=True, owner=owner)
    tg("editMessageText", chat_id=chat, message_id=msg["message_id"], text=status_text(state),
       parse_mode="HTML", reply_markup=menu_markup(state), quiet=True)


BOT_COMMANDS = [("menu", "Панель управления кнопками"), ("status", "Очередь и настройки"),
                ("help", "Все команды"), ("auto", "Автопубликация: on / good / off"),
                ("interval", "Интервал между постами, мин (0 — сразу)"),
                ("batch", "Выпустить по одной: /batch 15 или /batch 10 all"),
                ("pause", "Остановить все публикации"), ("resume", "Продолжить публикации"),
                ("night", "Ночь: /night 23 7 или /night off"), ("wm", "Водяной знак: on / off"),
                ("users", "Кто может управлять ботом"), ("allow", "Добавить: /allow 123 или @ник"),
                ("deny", "Убрать: /deny 123 или @ник"), ("access", "Кого слушать: list / group"),
                ("private", "Работа в личке: on / off"), ("drafts", "Куда черновики: group / private"),
                ("flow", "Что присылать: all / normal / top / elite"), ("alt", "Чередовать тяжёлые и добрые: on / off"),
                ("sources", "Источники: arab on|off, world on|off")]


def register_commands(state):
    """Список команд в меню «/» в группе модерации (один раз на версию)."""
    if state.get("commands_v") == 3 or not MOD_CHAT_ID:
        return
    cmds = [{"command": c, "description": d} for c, d in BOT_COMMANDS]
    if tg("setMyCommands", commands=cmds, scope={"type": "chat", "chat_id": MOD_CHAT_ID}) is not None:
        tg("setMyCommands", commands=cmds, scope={"type": "all_private_chats"})
        state["commands_v"] = 3


ACCESS_COMMANDS = ("/allow", "/deny", "/access", "/private", "/drafts", "/users")


def access_command(state, cmd, args, who, reply):
    """/allow и /deny (ID, @ник или ответом на сообщение человека), /access, /private, /drafts, /users."""
    s = state["settings"]
    arg = args[0].lower() if args else ""
    if cmd in ("/allow", "/deny"):
        target = (reply.get("from") or {}) if not args else {}
        uid = target.get("id") if target else (int(arg) if arg.lstrip("-").isdigit() else None)
        name = (target.get("username") or "").lower() if target else ("" if uid else arg.lstrip("@"))
        if not uid and not re.fullmatch(r"[a-z0-9_]{3,32}", name or ""):
            return ("⚠️ Пример: <code>/allow 123456789</code>, <code>/allow @nickname</code> "
                    "или ответь на сообщение человека командой <code>/allow</code>")
        ids, names = s.setdefault("allow_ids", []), s.setdefault("allow_names", [])
        if cmd == "/allow":
            if uid and uid not in ids:
                ids.append(uid)
            if name and name not in names:
                names.append(name)
            return f"✅ Добавлен: {uid or ''} {'@' + name if name else ''}".strip()
        if uid in ids:
            ids.remove(uid)
        if name in names:
            names.remove(name)
        return f"🚫 Убран: {uid or ''} {'@' + name if name else ''}".strip()
    if cmd == "/access":
        if arg not in ("group", "list", "группа", "список"):
            return "⚠️ <code>/access list</code> — только вписанные; <code>/access group</code> — вся группа «Модер»"
        s["access"] = "group" if arg in ("group", "группа") else "list"
        return "Доступ: " + ("вся группа «Модер»" if s["access"] == "group" else "только владелец и вписанные")
    if cmd == "/private":
        if arg not in ("on", "off"):
            return "⚠️ <code>/private on</code> — работать с ботом в личке; <code>/private off</code>"
        s["private_on"] = arg == "on"
        if s["private_on"] and CHAT["reply"] and str(CHAT["reply"]) != str(MOD_CHAT_ID):
            s["private_chat"] = CHAT["reply"]            # включили из лички — черновики сюда
        if not s["private_on"]:
            s["drafts_to"] = "group"
        return ("Личка включена: напишите боту в личку /start и /menu." if s["private_on"]
                else "Личка выключена, черновики — в группу «Модер».")
    if cmd == "/drafts":
        if arg not in ("group", "private"):
            return "⚠️ <code>/drafts group</code> — черновики в «Модер»; <code>/drafts private</code> — в личку"
        if arg == "private":
            s["private_on"] = True
            if CHAT["reply"] and str(CHAT["reply"]) != str(MOD_CHAT_ID):
                s["private_chat"] = CHAT["reply"]
        s["drafts_to"] = arg
        return "Новые черновики будут приходить " + ("в личку." if arg == "private" else "в группу «Модер».")
    return access_text(state)


def access_text(state):
    s = state["settings"]
    people = [f"владелец {i}" for i in sorted(ADMIN_IDS)] + [str(i) for i in s.get("allow_ids", [])] + \
        ["@" + n for n in s.get("allow_names", [])]
    return ("🔐 <b>Доступ</b>\n"
            f"Кого слушает в группе: {'всех участников «Модер»' if s.get('access') == 'group' else 'только список'}\n"
            f"Список: {', '.join(people) or '—'}\n"
            f"Работа в личке: {'вкл' if s.get('private_on') else 'выкл'}\n"
            f"Черновики приходят: {'в личку' if s.get('drafts_to') == 'private' and s.get('private_on') else 'в группу «Модер»'}")


def stop_auto(state):
    """После выключения автопилота (или «только добрые») снимаем из очереди то, что он уже поставил."""
    s, back = state["settings"], []
    for mid, q in list(state["queue"].items()):
        if q.get("auto") and not auto_allowed(s, {**state["drafts"].get(mid, {}), **q, "safe": True, "trusted": True}):
            state["queue"].pop(mid)
            state["drafts"][mid]["status"] = "pending"
            refresh(state, mid)
            back.append(mid)
    return f"↩️ Вернул на решение модератору: {len(back)}" if back else "Очередь автопилота в порядке."


def auto_mode_name(s):
    if not s["auto"]:
        return "выкл"
    if s.get("auto_top"):
        return f"🤖 только самое важное, до {s.get('per_hour', 3)} в час"
    return "🤖 вкл, все" if s.get("auto_hard", True) else "🤖 вкл, только добрые"


def status_data(state):
    """Настройки и цифры для панели на ПК."""
    s = state["settings"]
    return {"settings": s,
            "pending": sum(status_of(state, m) == "pending" and d.get("status") != "expired"
                           for m, d in state["drafts"].items()),
            "queue": len(state["queue"]),
            "auto_queue": sum(bool(q.get("auto")) for q in state["queue"].values()),
            "published_hour": sum(d.get("status") == "published" and NOW - d.get("published_at", 0) < 3600
                                  for d in state["drafts"].values()),
            "last_publish": state.get("last_publish", 0),
            "dupes_day": sum(NOW - x["t"] < 86400 for x in state.get("dupes", [])),
            "held_day": sum(NOW - x["t"] < 86400 for x in state.get("held", [])),
            "recent": [{"tone": x["tone"], "title": x["title"][:80], "t": x["t"]}
                       for x in state.get("published_log", [])[-6:]][::-1],
            "owners": sorted(ADMIN_IDS)}


def status_text(state):
    s = state["settings"]
    q = state["queue"].values()
    normal = [x for x in q if not x.get("at") and not x.get("auto")]
    sched = sorted(x["at"] for x in q if x.get("at"))
    pending = sum(d.get("status") == "pending" and m not in state["queue"]
                  for m, d in state["drafts"].items())
    night = f"{s['night'][0]}:00–{s['night'][1]}:00" if s.get("night") else "выключена"
    return (f"⚙️ <b>Настройки</b>\n"
            f"Публикации: {'⏸ на паузе' if s['paused'] else '▶️ идут'}\n"
            f"Ночь: {night}\n"
            f"Автопилот (проверенные — сразу): "
            f"{auto_mode_name(s)}\n"
            f"Между постами: {s['gap']} мин днём, {s['night_gap']} ночью\n"
            f"Водяной знак по умолчанию: {'да' if s['wm'] else 'нет'}\n"
            f"На модерацию: {FLOW_NAMES.get(s.get('flow'), 'всё')}\n"
            f"Чередование тяжёлых и добрых: {'вкл' if s.get('alternate', True) else 'выкл'}\n"
            f"Арабский мир: {'только доверенные' if s.get('arab_trusted') else 'все издания'} · "
            f"мир: {'только авторитетные' if s.get('world_reputable') else 'все издания'}\n"
            f"Последние посты: {''.join('🔴' if x['tone'] == 'hard' else '🟢' for x in state.get('published_log', [])[-8:]) or '—'}\n"
            f"За сутки отсеяно повторов: {sum(NOW - x['t'] < 86400 for x in state.get('dupes', []))}, "
            f"не прислано малоценных: {sum(NOW - x['t'] < 86400 for x in state.get('held', []))}\n\n"
            f"📝 Ждут решения: {pending}\n"
            f"📋 В очереди: {len(normal)} (🟢 {sum(x['tone'] == 'good' for x in normal)}, "
            f"🔴 {sum(x['tone'] == 'hard' for x in normal)})\n"
            f"🤖 Автопилот: {sum(bool(x.get('auto')) for x in q)}\n"
            f"🕒 Отложено: {len(sched)}" + (f" (ближайшая {fmt(sched[0])})" if sched else "") + "\n"
            f"Последний пост: {fmt(state['last_publish']) if state['last_publish'] else '—'}\n\n"
            + access_text(state))


def auto_allowed(s, d):
    """Может ли автопилот сам выпустить этот черновик при текущем режиме."""
    return (s["auto"] and d.get("safe") and d.get("trusted")                 # только доверенные источники
            and (s.get("auto_hard", True) or d.get("tone") != "hard")
            and (not s.get("auto_top") or d.get("importance", 3) >= 4))      # «самое важное»: 4–5 из 5


def autopilot_and_cleanup(state):
    s = state["settings"]
    top = s["auto"] and s.get("auto_top")
    waiting = sum(bool(q.get("auto")) for q in state["queue"].values())
    # в режиме «самое важное» сначала самые важные, и в очереди держим не больше одного — остальные ждут слота
    # чередование: новость другого тона, чем прошлая, получает +1 к важности (добрая 4 после тяжёлой = тяжёлая 5)
    prev = last_tone(state) if s.get("alternate", True) else None
    other = lambda d: prev is not None and (d.get("tone") == "hard") != (prev == "hard")
    drafts = sorted(state["drafts"].items(),
                    key=lambda kv: (-kv[1].get("importance", 3) - other(kv[1]), not other(kv[1]))) \
        if top else list(state["drafts"].items())
    for mid, d in drafts:
        if mid in state["queue"]:
            continue
        age_h = (NOW - d.get("created", NOW)) / 3600
        if d.get("status") == "pending":
            if auto_allowed(s, d) and age_h < AUTO_MAX_AGE_HOURS and not (top and waiting >= 1):
                state["queue"][mid] = {"tone": d.get("tone", ""), "approved_at": NOW, "auto": True,
                                       "importance": d.get("importance", 3),
                                       "msg": d.get("snap") or {"kind": "text", "text": "", "entities": []}}
                d["status"] = "queued"
                waiting += 1
                refresh(state, mid)
            elif age_h > PENDING_TTL_HOURS:
                d["status"] = "expired"
                refresh(state, mid)
        if d.get("status") != "pending" and age_h > 24 * 7:
            del state["drafts"][mid]


# ---------------------- водяной знак ----------------------

def find_font(size):
    from PIL import ImageFont
    for path in ("watermark.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def make_mark(width):
    """Водяной знак под картинку шириной width: логотип или «таблетка» с текстом."""
    from PIL import Image, ImageDraw
    if os.path.exists("watermark.png"):
        logo = Image.open("watermark.png").convert("RGBA")
        w = max(60, int(width * 0.18))
        logo = logo.resize((w, int(logo.height * w / logo.width)))
        logo.putalpha(logo.getchannel("A").point(lambda a: int(a * 0.85)))
        return logo

    font = find_font(max(14, int(width * 0.032)))
    box = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), WATERMARK_TEXT, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    px, py = int(th * 0.8), int(th * 0.45)
    mark = Image.new("RGBA", (tw + 2 * px, th + 2 * py), (0, 0, 0, 0))
    d = ImageDraw.Draw(mark)
    d.rounded_rectangle((0, 0, mark.width - 1, mark.height - 1),
                        radius=mark.height // 2, fill=(0, 0, 0, 95))
    d.text((px - box[0], py - box[1]), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 230))
    return mark


def watermark_photo(data):
    from PIL import Image
    img = Image.open(BytesIO(data)).convert("RGB")
    mark = make_mark(img.width)
    margin = int(img.width * 0.025)
    img.paste(mark, (img.width - mark.width - margin, img.height - mark.height - margin), mark)
    out = BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def watermark_video(data, width):
    import imageio_ffmpeg
    width = width or 1280
    with tempfile.TemporaryDirectory() as tmp:
        src, png, dst = (os.path.join(tmp, n) for n in ("in.mp4", "mark.png", "out.mp4"))
        open(src, "wb").write(data)
        make_mark(width).save(png)
        margin = int(width * 0.025)
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", src, "-i", png,
               "-filter_complex", f"overlay=W-w-{margin}:H-h-{margin}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
               "-c:a", "copy", "-movflags", "+faststart", dst]
        try:
            subprocess.run(cmd, check=True, timeout=420)
        except Exception as e:
            print("ffmpeg не справился:", e)
            return None
        out = open(dst, "rb").read()
        return out if len(out) < 49 * 1024 * 1024 else None


def telegram_file(file_id):
    """Скачивает файл из Telegram (боту доступны файлы до 20 МБ)."""
    info = tg("getFile", file_id=file_id, quiet=True)
    if not info or not info.get("file_path"):
        return None
    return download(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}", 20)


def send_watermarked(snap):
    data = telegram_file(snap["file_id"])
    if not data:
        return None
    common = {"chat_id": CHANNEL_ID, "caption": snap["text"], "caption_entities": snap["entities"]}
    if snap["kind"] == "photo":
        return tg("sendPhoto", files={"photo": ("photo.jpg", watermark_photo(data))}, **common)
    video = watermark_video(data, snap.get("width"))
    if not video:
        return None
    return tg("sendVideo", files={"video": ("video.mp4", video)}, supports_streaming=True,
              width=snap.get("width"), height=snap.get("height"), **common)


# ---------------------- GLM: дешёвый отбор новостей и помощник в группе ----------------------

ZAI_KEY = os.environ.get("ZAI_API_KEY", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "isa_news_bot")
# По очереди, пока кто-то не ответит: бесплатный z.ai → OpenRouter (с баланса)
GLM_CHAIN = [
    ("z.ai", "https://api.z.ai/api/paas/v4/chat/completions", "zai", "glm-4.7-flash"),
    ("z.ai", "https://api.z.ai/api/paas/v4/chat/completions", "zai", "glm-4.5-flash"),
    ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions", "or", "z-ai/glm-5.3-flash"),
]
TRIAGE_EVERY_MIN = 30        # как часто GLM отбирает свежие новости


class GLMUnavailable(Exception):
    pass


def glm(messages, tools=None, max_tokens=6000, temperature=None):
    """Запрос к GLM. Возвращает (сообщение, кто ответил) или бросает GLMUnavailable."""
    errors = []
    for name, url, which, model in GLM_CHAIN:
        key = ZAI_KEY if which == "zai" else OPENROUTER_KEY
        if not key:
            continue
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        if which == "zai":
            body["thinking"] = {"type": "disabled"}
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=150)
            j = r.json()
            if r.ok and j.get("choices"):
                return j["choices"][0]["message"], f"{name} {model}"
            errors.append(f"{name} {model}: {str(j.get('error', j))[:100]}")
        except Exception as e:
            errors.append(f"{name} {model}: {str(e)[:100]}")
    raise GLMUnavailable("; ".join(errors) or "нет ключей GLM")


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else {}


TRIAGE_PROMPT = """Ты отбираешь новости для канала @ilm4_info. Правила канала — в системном сообщении.
Ниже свежие записи из лент (заголовки на разных языках). Выбери ВСЕ, что подходит каналу по темам
и позиции (число не ограничено), остальное пропусти.
- Темы канала: ислам и мусульмане, Харамайн, хадж и умра (цены, правила, даты), мечети, муфтии и учёные,
  положение мусульман (Палестина, Сирия, Йемен, Судан и др.), помощь КСА, решения исламских стран.
- МУСУЛЬМАНЕ ПО ВСЕМУ МИРУ — тоже наша тема: Европа, Америка, Россия, Индия, Китай, Африка — запреты
  никаба и хиджаба, мечети, нападения на мусульман, законы о мигрантах-мусульманах, суды, выборы мусульман,
  халяль, Коран. Такие новости бери.
- ТЯЖЁЛОЕ БЕРИ ОБЯЗАТЕЛЬНО: атаки хуситов и их перехват, Газа и Аль-Акса, удары и вторжения Израиля
  в Сирии и Ливане, Иран, КСИР и Ормуз, ИГИЛ и «Аль-Каида», аресты и суды над людьми Асада, взрывы,
  притеснение мусульман (запреты хиджаба, закрытие медресе). Сомнение — бери с label "verify".
- ОБЯЗАТЕЛЬНО бери: разбор и опровержение фейков (власти, агентства или фактчекеры опровергли ложь
  о Харамайне, КСА, мусульманах); разоблачения схем и деятельности ХАМАС, хуситов, «Хизбаллы», Ирана и КСИР,
  ихвана, ИГИЛ, «Аль-Каиды», шиитских ополчений и других сект — финансирование, контрабанда, вербовка,
  аресты ячеек, суды — из доверенных источников (✓).
- НЕ бери: мелочи и курьёзы (животные, бытовые истории, «необычный случай»), спорт, погоду
  (кроме Мекки и Медины), бизнес, туризм, развлечения, бытовой криминал,
  светскую политику без связи с мусульманскими странами и регионом.
- Политику высшего уровня (король, наследный принц, главы государств, министры иностранных дел и обороны,
  верховные муфтии, имамы Харамайна: встречи, звонки, визиты, поздравления, осуждения) — бери.
- Сильные примеры: король Марокко осудил атаки хуситов на КСА; в Сирии задержан генерал прежнего режима,
  виновный в массовом убийстве; министры обороны КСА и Малайзии провели переговоры; в Самарканде пройдёт
  Всемирный форум вакфов.
- Слабые («ну и что?»): школьники едут на олимпиаду, замминистра рассказал в ООН об опыте, премия по туризму,
  мелкий протокол чиновников. Их тоже можно взять, но в why напиши «малоценная».
- Повторы: одну историю бери один раз — лучше из источника с пометкой ✓.
- Источники с ✓ (саудовские, официальные агентства, мировые агентства) — в приоритете: их достойные новости
  бери все. Арабский мир — у ✓; Запад и Европа — у их серьёзных изданий; СНГ — у местных надёжных.
- Бери новость у ПЕРВОИСТОЧНИКА. Местные издания (Узбекистан, Таджикистан, Казахстан, Кыргызстан, Египет,
  Ливан, Иордания и т. п.) — только для новостей своей страны и региона. Если такое издание пересказывает
  мировую новость (США, Иран, Израиль, Газа, Саудия…) — НЕ бери её: она придёт от Reuters, AP, саудовских
  или западных изданий.
- label "clear" — только если источник помечен ✓ И факты однозначные (официальное сообщение).
  Заявления воюющих сторон, слухи, спорные цифры, острая политика — "verify".
- tone: "good" — добрая или нейтральная, "hard" — тяжёлая.
Ответь ТОЛЬКО JSON без пояснений:
{{"picks": [{{"index": 12, "label": "clear", "tone": "good", "why": "коротко по-русски"}}]}}

Записи:
{items}"""


def glm_warn(state, reason):
    """Раз в 6 часов предупреждаем в группе, что GLM не работает и отбор делает Claude."""
    if NOW - state.get("glm_warned", 0) > 6 * 3600:
        say("⚠️ <b>GLM сейчас недоступен</b> — отбор новостей делает Claude (это больше расходует подписку).\n"
            f"<i>{html.escape(reason[:300])}</i>")
        state["glm_warned"] = NOW


def number_shortlist(state, short):
    """У каждой отобранной новости — короткий номер (Н12), чтобы модератор мог на неё сослаться."""
    for x in short:
        if not x.get("sid"):
            state["sid"] = state.get("sid", 0) + 1
            x["sid"] = state["sid"]
    return short


def triage(state, force=False):
    """GLM раз в 30 минут читает все свежие новости и отбирает подходящие в shortlist.json.
    Облачный Claude потом берёт только их: переводит, а сомнительные — перепроверяет."""
    if not (ZAI_KEY or OPENROUTER_KEY):
        return
    if not force and NOW - state.get("last_triage", 0) < TRIAGE_EVERY_MIN * 60 - 120:
        return
    state["last_triage"] = NOW
    items, _ = gather("triage_seen.json", settings=state["settings"])

    pool = [x for x in read_json("pool.json", []) if NOW - x["t"] < 8 * 3600]   # для перепроверки
    pool += [{"title": it["title"], "source": it["source"], "link": it["link"], "t": NOW} for it in items]
    write_json("pool.json", pool[-2000:])
    if not items:
        state["triage_ok"] = NOW
        return

    rules = open("rules.md", encoding="utf-8").read()
    picks, who = [], ""
    try:
        for start in range(0, len(items), 120):   # порциями, чтобы модель не запуталась
            part = items[start:start + 120]
            listing = "\n".join(f"[{it['index']}] ({it['source']}{' ✓' if it['source'] in TRUSTED_SOURCES else ''}) "
                                 f"{it['title']}" + (f" — {it['summary'][:150]}" if it["summary"] else "") for it in part)
            msg, who = glm([{"role": "system", "content": rules},
                            {"role": "user", "content": TRIAGE_PROMPT.format(items=listing)}])
            picks += parse_json(msg.get("content")).get("picks", [])
    except GLMUnavailable as e:
        print("GLM недоступен:", e)
        state["last_triage"] = 0   # попробуем в следующий раз
        return glm_warn(state, str(e))
    except (ValueError, AttributeError) as e:
        print("GLM ответил не по формату:", e)
        return

    short = [x for x in read_json("shortlist.json", []) if NOW - x["t"] < 12 * 3600]
    have = {x["id_link"] for x in short}
    added = 0
    for p in picks:
        i = p.get("index")
        if not isinstance(i, int) or not 0 <= i < len(items) or items[i]["id_link"] in have:
            continue
        it = items[i]
        trusted = it["source"] in TRUSTED_SOURCES
        # «только перевод» разрешаем лишь надёжным источникам — это проверяет код, а не GLM
        label = "clear" if p.get("label") == "clear" and trusted else "verify"
        state["sid"] = state.get("sid", 0) + 1
        short.append({**it, "label": label, "tone": p.get("tone", ""), "why": str(p.get("why", ""))[:200], "t": NOW,
                      "sid": state["sid"]})
        have.add(it["id_link"])
        added += 1
    write_json("shortlist.json", number_shortlist(state, short))
    state["triage_ok"] = NOW
    print(f"GLM ({who}) отобрал {added} из {len(items)} свежих новостей")


# ---------- помощник: команды словами в группе модерации ----------

BOT_CALL = re.compile(r"^\s*бот\b|@" + re.escape(BOT_USERNAME), re.I)

ASSISTANT_TOOLS = [
    {"type": "function", "function": {"name": "publish_now", "description": "Сразу опубликовать черновики в канал",
     "parameters": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}}},
    {"type": "function", "function": {"name": "approve", "description": "Поставить черновики в обычную очередь (выйдут по одному с интервалом)",
     "parameters": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}}},
    {"type": "function", "function": {"name": "reject", "description": "Отклонить черновики (или снять из очереди)",
     "parameters": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}}},
    {"type": "function", "function": {"name": "schedule", "description": "Отложить черновик на время (Ташкент)",
     "parameters": {"type": "object", "properties": {"id": {"type": "integer"}, "when": {"type": "string", "description": "«21:30» или «27.09 21:30»"}}, "required": ["id", "when"]}}},
    {"type": "function", "function": {"name": "edit_text", "description": "Заменить текст черновика (HTML: <b>, <i>, <a href>). Смысл и факты не менять.",
     "parameters": {"type": "object", "properties": {"id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["id", "text"]}}},
    {"type": "function", "function": {"name": "find_news", "description": "Найти свежие новости сейчас (GLM отбирает, заголовки переводятся на русский) и показать список с номерами Н1, Н2…",
     "parameters": {"type": "object", "properties": {"hours": {"type": "integer", "description": "за сколько последних часов показать (по умолчанию 3)"}}}}},
    {"type": "function", "function": {"name": "request_drafts", "description": "Попросить Claude сделать черновики из найденных новостей (номера Н из find_news) — в первую очередь",
     "parameters": {"type": "object", "properties": {"news": {"type": "array", "items": {"type": "integer"}}}, "required": ["news"]}}},
    {"type": "function", "function": {"name": "spread", "description": "Выпустить по одной с интервалом: первую сразу, дальше каждые N минут. ids — номера черновиков; без ids — всё, что уже в очереди",
     "parameters": {"type": "object", "properties": {"minutes": {"type": "integer"}, "ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["minutes"]}}},
    {"type": "function", "function": {"name": "settings", "description": "Изменить настройки публикации",
     "parameters": {"type": "object", "properties": {
         "gap": {"type": "integer", "description": "минут между постами (0 — всё сразу)"},
         "night_gap": {"type": "integer", "description": "минут между постами ночью"},
         "night_start": {"type": "integer"}, "night_end": {"type": "integer"},
         "night_off": {"type": "boolean"}, "auto": {"type": "boolean", "description": "автопубликация проверенных"},
         "auto_hard": {"type": "boolean", "description": "автопубликация и для тяжёлых новостей (false — тяжёлые ждут модератора)"},
         "auto_top": {"type": "boolean", "description": "режим «самое важное»: сам публикует только самые важные"},
         "per_hour": {"type": "integer", "description": "сколько самых важных в час в режиме auto_top"},
         "watermark": {"type": "boolean"}, "paused": {"type": "boolean"},
         "flow": {"type": "string", "enum": ["all", "normal", "top", "elite"],
                  "description": "что присылать на модерацию: всё / без малоценных / только самое важное / "
                                 "только самое-самое (главное, большая политика, фейки, разоблачения)"},
         "alternate": {"type": "boolean", "description": "чередовать тяжёлые и добрые в канале"},
         "arab_trusted": {"type": "boolean", "description": "арабский мир только из доверенных источников"},
         "world_reputable": {"type": "boolean", "description": "по миру только авторитетные издания"}}}}},
]

ASSISTANT_SYSTEM = """Ты — помощник модератора русскоязычного канала @ilm4_info (новости исламского мира,
саляфитский манхадж, поддержка Королевства Саудовская Аравия). Ты управляешь ботом по просьбам модератора
через функции. Отвечай по-русски, коротко и по делу.
- Черновики называй по номеру и заголовку. Если модератор ответил на черновик — речь о нём.
- «Опубликуй», «публикуй», «выложи», «кидай» — это publish_now (СРАЗУ в канал, не в очередь);
  «в очередь», «потом» — approve; «не надо», «убери» — reject; «на 21:00» — schedule.
- «Сделай оформление / поправь текст» — edit_text: меняй только форму (разметку, порядок, эмодзи, хэштеги),
  НЕ меняй смысл, цифры, имена и факты; сохраняй строку «Подписаться | Источник» со ссылками.
- «Выложи 53, 55, 58 раз в 10 минут», «очередь по одной каждые 15 минут» — spread (разово для этих новостей).
- «Теперь всегда публикуй раз в 30 минут» — settings gap=30; «публикуй сразу» — settings gap=0.
- «Отключи автопубликацию» — settings auto=false; «тяжёлые сам не публикуй» — auto=true, auto_hard=false;
  «включи всё обратно» — auto=true, auto_hard=true, auto_top=false; «стоп, ничего не публикуй» — paused=true.
- «Публикуй только самое важное, 2–3 в час» — auto=true, auto_hard=true, auto_top=true, per_hour=3.
- «Только самое-самое», «только главное, без мелочей вообще» — flow="elite".
- «Присылай только важное» — flow="top"; «без мелочи» — flow="normal"; «присылай всё» — flow="all".
- «Чередуй тяжёлые с добрыми» — alternate=true.
- «Арабские только из доверенных» — arab_trusted=true; «по миру только серьёзные издания» — world_reputable=true.
- «Найди новости», «что нового» — find_news; покажи список как есть (номер Н, источник, заголовок, метка).
- «Сделай черновики из Н2 и Н5», «переведи эти» — request_drafts. Перевод и перепроверку делает Claude,
  сам новости не переводи и не пиши — черновики придут в группу после ближайшего запуска Claude.
- Если непонятно, о каком черновике речь, — переспроси. Ничего не выдумывай.
Сейчас в Ташкенте: {now}. Настройки: {settings}
Черновики (номер · статус · тон · проверка · заголовок):
{drafts}"""


def draft_title(d):
    text = (d.get("snap") or {}).get("text") or ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text.split("\n")[0]))[:90] or "(без текста)"


def assistant(state, msg):
    """Модератор пишет «Бот, …» — GLM понимает и выполняет."""
    lines = []
    for mid, d in sorted(state["drafts"].items(), key=lambda kv: -kv[1].get("created", 0)):
        st = status_of(state, mid)
        if st in ("pending", "queued", "auto", "scheduled") and NOW - d.get("created", NOW) < 48 * 3600:
            lines.append(f"{mid} · {st} · {d.get('tone') or '-'} · {'safe' if d.get('safe') else 'проверить'} · {draft_title(d)}")
    s = state["settings"]
    system = ASSISTANT_SYSTEM.format(now=fmt(NOW), settings=json.dumps(s, ensure_ascii=False),
                                     drafts="\n".join(lines[:60]) or "(нет)")
    text = msg.get("text") or ""
    orig = msg.get("reply_to_message") or {}
    okey = key_of(msg["chat"]["id"], orig["message_id"]) if orig.get("message_id") else ""
    if okey in state["drafts"]:
        text += f"\n(модератор ответил на черновик {okey})"
    history = state.setdefault("chat", [])[-8:]
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": text}]
    try:
        for _ in range(6):
            reply, who = glm(messages, ASSISTANT_TOOLS, max_tokens=3000)
            calls = reply.get("tool_calls") or []
            if not calls:
                break
            messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    result = run_tool(state, c["function"]["name"], args)
                except Exception as e:
                    result = f"ошибка: {e}"
                messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": result})
        answer = (reply.get("content") or "Готово.").strip()
    except GLMUnavailable as e:
        return say("⚠️ GLM сейчас недоступен, команды словами не работают. Пользуйся кнопками и /help.\n"
                   f"<i>{html.escape(str(e)[:200])}</i>", msg["message_id"])
    tg("sendMessage", chat_id=msg["chat"]["id"], text=answer[:4000], reply_parameters={"message_id": msg["message_id"]})
    state["chat"] = (history + [{"role": "user", "content": text}, {"role": "assistant", "content": answer}])[-10:]


def run_tool(state, name, a):
    """Выполняет действие, о котором попросил модератор. Возвращает отчёт для GLM."""
    def draft(i):
        mid = str(i)
        if mid not in state["drafts"]:
            raise ValueError(f"нет черновика {i}")
        return mid, state["drafts"][mid]

    def item_for(mid, d, **extra):
        snap = (state["queue"].get(mid) or {}).get("msg") or d.get("snap")
        if not snap:
            raise ValueError(f"черновик {mid} нельзя опубликовать: нет его копии")
        return {"tone": d.get("tone", ""), "approved_at": NOW, "msg": snap, **extra}

    out = []
    if name in ("publish_now", "approve", "reject"):
        for i in a.get("ids", []):
            try:
                mid, d = draft(i)
                if d.get("status") == "published":
                    out.append(f"{i}: уже опубликован")
                elif name == "reject":
                    state["queue"].pop(mid, None)
                    d["status"] = "rejected"
                    refresh(state, mid)
                    out.append(f"{i}: отклонён")
                else:
                    state["queue"][mid] = item_for(mid, d)
                    d["status"] = "queued"
                    if name == "publish_now":
                        publish_one(state, mid, state["queue"][mid])
                        out.append(f"{i}: {'опубликован' if d.get('status') == 'published' else 'не удалось опубликовать'}")
                    else:
                        refresh(state, mid)
                        out.append(f"{i}: в очереди")
            except ValueError as e:
                out.append(str(e))
    elif name == "schedule":
        mid, d = draft(a["id"])
        at = parse_at(str(a.get("when", "")).split())
        if not at:
            return "не понял время — нужно «21:30» или «27.09 21:30»"
        state["queue"][mid] = item_for(mid, d, at=at)
        d["status"] = "queued"
        refresh(state, mid)
        out.append(f"{a['id']}: выйдет {fmt(at)}")
    elif name == "edit_text":
        mid, d = draft(a["id"])
        snap = d.get("snap") or {}
        chat, msg_id = where(state, mid)
        base = {"chat_id": chat, "message_id": msg_id, "parse_mode": "HTML",
                "reply_markup": keyboard(d, status_of(state, mid), state["wm"].get(mid, state["settings"]["wm"]),
                                         (state["queue"].get(mid) or {}).get("at"))}
        if snap.get("kind", "text") == "text":
            res = tg("editMessageText", text=a["text"], **base)
        else:
            res = tg("editMessageCaption", caption=a["text"][:1024], **base)
        if not res:
            return "не получилось изменить текст (проверь HTML-разметку и длину: у фото/видео до 1024 символов)"
        d["snap"] = snapshot(res)
        if mid in state["queue"]:
            state["queue"][mid]["msg"] = d["snap"]
        out.append(f"{a['id']}: текст изменён")
    elif name == "find_news":
        triage(state, force=True)
        hours = a.get("hours") or 3
        short = number_shortlist(state, read_json("shortlist.json", []))
        write_json("shortlist.json", short)
        items = [x for x in short if NOW - x["t"] < hours * 3600]
        if not items:
            return "свежих подходящих новостей не нашлось (или GLM недоступен)"
        translate_titles(items)
        return "\n".join(f"Н{x.get('sid', '?')} · {'✅' if x['label'] == 'clear' else '🔎'} · {x['source']} · "
                         f"{x.get('title_ru') or x['title']}" + (" · ⭐ уже заказан" if x.get("priority") else "")
                         for x in items[-40:])
    elif name == "request_drafts":
        want = {int(n) for n in a.get("news", [])}
        short = read_json("shortlist.json", [])
        hit = [x for x in short if x.get("sid") in want]
        for x in hit:
            x["priority"] = True
        write_json("shortlist.json", short)
        state["priority_since"] = NOW
        return (f"заказано черновиков: {len(hit)} (Н{', Н'.join(str(x['sid']) for x in hit)}). "
                "Claude возьмёт их первыми при ближайшем запуске (в :15 или :45)") if hit else "таких номеров нет"
    elif name == "spread":
        return re.sub(r"<[^>]+>", "", spread(state, max(1, int(a.get("minutes") or 15)), a.get("ids") or []))
    elif name == "settings":
        s = state["settings"]
        for k, v in a.items():
            if k in ("gap", "night_gap") and isinstance(v, int) and v >= 0:
                s[k] = v
            elif k in ("auto", "auto_hard", "auto_top", "alternate", "arab_trusted", "world_reputable"):
                s[k] = bool(v)
            elif k == "flow" and v in FLOW_MIN:
                s["flow"] = v
            elif k == "per_hour" and isinstance(v, int) and v > 0:
                s["per_hour"] = min(12, v)
            elif k == "watermark":
                s["wm"] = bool(v)
            elif k == "paused":
                s["paused"] = bool(v)
            elif k == "night_off" and v:
                s["night"] = None
        if isinstance(a.get("night_start"), int) and isinstance(a.get("night_end"), int):
            s["night"] = [a["night_start"] % 24, a["night_end"] % 24]
        out.append(stop_auto(state))
        out.append("настройки: " + json.dumps(s, ensure_ascii=False))
    else:
        return f"нет такого действия: {name}"
    return "; ".join(out)


# ---------------------- дежурный: публикация ----------------------

def alternate(items, last_tone):
    """Порядок публикации: чередуем добрые и тяжёлые, начиная с противоположной прошлой."""
    good = [x for x in items if x[1]["tone"] != "hard"]
    hard = [x for x in items if x[1]["tone"] == "hard"]
    out, take_hard = [], last_tone != "hard"
    while good or hard:
        src = hard if (take_hard and hard) or not good else good
        out.append(src.pop(0))
        take_hard = not take_hard
    return out


def last_tone(state):
    log = state.get("published_log") or []
    return log[-1]["tone"] if log else state.get("last_tone", "")


def tone_ok(state, item):
    """Чередование: после тяжёлой следующая тяжёлая (от автопилота) ждёт ALT_HOLD_MIN минут,
    если за это время не вышла добрая. Добрые — в любое время. Одобренное ✅ вручную — как решил модератор."""
    if not state["settings"].get("alternate", True) or not item.get("auto") or item.get("tone") != "hard":
        return True
    log = state.get("published_log") or []
    return not log or log[-1]["tone"] != "hard" or NOW - log[-1]["t"] >= ALT_HOLD_MIN * 60


def return_stale_auto(state):
    """Автопилот не выпустил вовремя (ждали чередования или слота) — старое возвращаем модератору."""
    for mid, q in list(state["queue"].items()):
        d = state["drafts"].get(mid, {})
        if q.get("auto") and NOW - float(d.get("created", NOW)) > AUTO_MAX_AGE_HOURS * 3600:
            state["queue"].pop(mid)
            d["status"] = "pending"
            refresh(state, mid)


def publish(state):
    s = state["settings"]
    if s["paused"]:
        return
    return_stale_auto(state)
    queue = sorted(state["queue"].items(), key=lambda kv: kv[1]["approved_at"])

    # 1) Отложенные — точно ко времени, в любое время суток (все, что подошли)
    for mid, item in sorted(((m, q) for m, q in queue if q.get("at") and q["at"] <= NOW), key=lambda kv: kv[1]["at"]):
        publish_one(state, mid, item)

    # 2) Проверенные Claude (автопилот) и одобренные ✅ — при /интервал 0 сразу все (будем первыми),
    #    при /интервал 15 — по одной раз в 15 минут. Чередуем: добрая — тяжёлая — добрая…
    #    Ночью одобренные вручную ждут утра, проверенные выходят.
    night = is_night(s)
    gap = s["night_gap"] if night else s["gap"]
    ready = [(m, q) for m, q in queue if not q.get("at") and m in state["queue"] and (q.get("auto") or not night)]
    if s["auto"] and s.get("auto_top"):
        autos = [x for x in ready if x[1].get("auto")]
        ready = [x for x in ready if not x[1].get("auto")]
        slot = 3600 / max(1, s.get("per_hour", 3))
        autos = [x for x in autos if tone_ok(state, x[1])]
        if autos and NOW - state.get("last_auto_publish", 0) >= slot - 90:
            mid, item = max(autos, key=lambda kv: kv[1].get("importance", 3))
            publish_one(state, mid, item)
            state["last_auto_publish"] = NOW
    for mid, item in alternate(ready, last_tone(state)):
        if gap and NOW - state["last_publish"] < gap * 60 - 90:
            break
        if not tone_ok(state, item):
            continue
        publish_one(state, mid, item)
        if gap:
            break
        time.sleep(2)


def publish_one(state, mid, item):
    snap = item["msg"]
    res = None
    if state["wm"].get(mid, state["settings"]["wm"]) and snap["kind"] != "text":
        try:
            res = send_watermarked(snap)
        except Exception as e:
            print("Ошибка водяного знака:", repr(e))
        if not res:
            print("Водяной знак не получился — публикую без него")
    if not res:
        chat, msg_id = where(state, mid)
        res = tg("copyMessage", chat_id=CHANNEL_ID, from_chat_id=chat,
                 message_id=msg_id, reply_markup={"inline_keyboard": []})
    if not res:
        return
    del state["queue"][mid]
    state["drafts"].setdefault(mid, {}).update(status="published", published_at=NOW)
    state["last_publish"] = NOW
    state["last_tone"] = item["tone"]
    state["published_log"].append({"t": NOW, "tone": item.get("tone", ""), "title": title_of(snap.get("text")),
                                   "importance": item.get("importance", 3), "auto": bool(item.get("auto"))})
    refresh(state, mid, "published")
    print("Опубликовано:", snap["text"][:80])


# ---------------------- дежурный: приём черновиков ----------------------

def send_draft(state, e):
    """Отправляет черновик в группу модерации и запоминает его.
    e: text, link, image, video, tone, safe, note."""
    d = {"tone": e.get("tone", ""), "url": e["link"], "created": NOW, "status": "pending"}
    if "safe" in e:
        d.update(safe=bool(e["safe"]), note=(e.get("note") or "")[:40], trusted=bool(e.get("trusted")),
                 importance=e.get("importance", 3))
    wm = state["settings"]["wm"]
    chat = work_chat(state)          # группа «Модер» или личка — по настройке
    base = {"chat_id": chat, "parse_mode": "HTML"}
    text, image, video, msg = e["text"], e.get("image"), e.get("video"), None

    if len(text) <= 1024:   # лимит подписи к фото/видео
        kb = keyboard({**d, "media": True}, "pending", wm)
        if video:
            msg = tg("sendVideo", video=video, caption=text, supports_streaming=True, reply_markup=kb, **base)
            if not msg and not DRY_RUN and (data := download(video, 49)):
                msg = tg("sendVideo", files={"video": ("video.mp4", data)}, caption=text,
                         supports_streaming=True, reply_markup=kb, **base)
        if not msg and image:
            msg = tg("sendPhoto", photo=image, caption=text, reply_markup=kb, **base)
            if not msg and not DRY_RUN and (data := download(image, 9)):
                # Некоторые сайты не отдают картинку серверам Telegram — качаем сами
                msg = tg("sendPhoto", files={"photo": ("photo.jpg", data)}, caption=text,
                         reply_markup=kb, **base)
    if msg:
        d["media"] = True
    else:
        preview = ({"url": image, "prefer_large_media": True, "show_above_text": True}
                   if image else {"is_disabled": True})
        msg = tg("sendMessage", text=text, link_preview_options=preview,
                 reply_markup=keyboard(d, "pending", wm), **base)
    if msg:
        d["snap"] = snapshot(msg) if not DRY_RUN else {"kind": "text", "text": text, "entities": []}
        key = key_of(chat, msg["message_id"])
        d.update(chat=chat, msg_id=msg["message_id"])
        state["drafts"][key] = d
        if not DRY_RUN:   # номер черновика узнаём только после отправки
            d["n"] = key
            refresh(state, key, "pending")
    return msg


def inbox_files():
    """Файлы черновиков из ветки claude/inbox: [(имя, список черновиков)]."""
    if INBOX_LOCAL or DRY_RUN:
        folder = os.path.join(INBOX_DIR, "inbox")
        names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        return [(n, read_json(os.path.join(folder, n), [])) for n in names]
    if git("fetch", "-q", "--depth=1", "origin", INBOX_BRANCH).returncode != 0:
        return []   # ветки ещё нет — редактор ничего не присылал
    names = git("ls-tree", "--name-only", "FETCH_HEAD", "inbox/").stdout.split()
    out = []
    for path in sorted(names):
        try:
            out.append((os.path.basename(path), json.loads(git("show", f"FETCH_HEAD:{path}").stdout)))
        except Exception as e:
            print("Не прочитал", path, e)
    return out


def ingest_inbox(state):
    sent = 0
    for name, drafts in inbox_files():
        if name in state["inbox_done"]:
            continue
        for e in screen_drafts(state, drafts):
            if send_draft(state, e):
                sent += 1
                time.sleep(2)
        state["inbox_done"].append(name)
    if sent:
        print(f"Черновиков отправлено в модерацию: {sent}")


# ---------- повторы: не присылать и не публиковать то, что уже было за 3 дня ----------

DUP_STOP = set("и в во на по с со о об от до за из к у не что как для это его их при после под над без или "
               "также года году тысяч более около свыше".split())


def title_of(text):
    """Заголовок из текста поста (первая строка, без разметки и эмодзи)."""
    first = re.sub(r"<[^>]+>", "", (text or "").split("\n")[0])
    return re.sub(r"^[^\w«\"]+", "", first).strip()


def title_words(title):
    t = re.sub(r"[^\w\s-]", " ", title.lower().replace("ё", "е"))
    return {w[:5] for w in t.split() if len(w) > 2 and w not in DUP_STOP and not w.isdigit()} | \
        set(re.findall(r"\d{2,}", t))


def taken_titles(state, hours=DUP_HOURS):
    """Всё, что брали за 3 дня: черновики (любой статус) и опубликованное."""
    out = [(title_of((d.get("snap") or {}).get("text")), d.get("status", "")) for d in state["drafts"].values()
           if NOW - float(d.get("created", 0)) < hours * 3600]
    out += [(x["title"], "published") for x in state.get("published_log", []) if NOW - x["t"] < hours * 3600]
    return [(t, st) for t, st in dict(out).items() if t]


def similarity(a, b, idf):
    A, B = title_words(a), title_words(b)
    if len(A) < 2 or len(B) < 2:
        return 0
    return sum(idf(w) for w in A & B) / min(sum(idf(w) for w in A), sum(idf(w) for w in B))


def make_idf(titles):
    import math
    df = {}
    for t in titles:
        for w in title_words(t):
            df[w] = df.get(w, 0) + 1
    n = len(titles) + 1
    return lambda w: math.log(n / (1 + df.get(w, 0))) + 0.5


DUP_SURE = 0.8               # так похожи — точно повтор (проверено на заголовках канала за 3 дня)


def similar_taken(state, titles, low=0.45):
    """Для каждого заголовка — похожие из взятого за 3 дня (и из этой же пачки): [(сходство, заголовок)]."""
    taken = taken_titles(state)
    idf = make_idf([t for t, _ in taken] + titles)
    out = []
    for i, t in enumerate(titles):
        pool = taken + [(x, "draft") for x in titles[:i]]
        near = sorted(((similarity(t, x, idf), x) for x, _ in pool), reverse=True)[:3]
        out.append([(round(v, 2), x) for v, x in near if v >= low])
    return out


def find_repeats(state, titles):
    """Страховка: почти одинаковое со взятым за 3 дня — {номер: похожий заголовок}.
    Тонкие случаи (то же событие другими словами) отсеивает Claude-редактор командой check."""
    return {i: near[0][1] for i, near in enumerate(similar_taken(state, titles)) if near and near[0][0] >= DUP_SURE}


def screen_drafts(state, drafts):
    """Перед отправкой в модерацию: поток (важность) и повторы. Заказанное модератором и свои — всегда."""
    s, now_t = state["settings"], NOW
    keep = []
    for e in drafts:
        if not e.get("manual") and "safe" in e and not flow_pass(s.get("flow", "all"), e):
            state["held"].append({"t": now_t, "title": title_of(e["text"]), "importance": e.get("importance", 3)})
            continue
        keep.append(e)
    check = [e for e in keep if not e.get("manual")]
    repeats = find_repeats(state, [title_of(e["text"]) for e in check]) if check else {}
    for i, like in repeats.items():
        state["dupes"].append({"t": now_t, "title": title_of(check[i]["text"]), "like": like})
        print(f"Повтор, не присылаю: «{title_of(check[i]['text'])}» ≈ «{like}»")
    drop = {id(check[i]) for i in repeats}
    return [e for e in keep if id(e) not in drop]


# ---------------------- редактор: сбор новостей ----------------------

def clean(raw):
    text = re.sub(r"<br\s*/?>", "\n", raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(text)).strip()


def meta(page, prop):
    """Достаёт <meta property="og:image" content="..."> и подобные."""
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        if re.search(rf'(?:property|name)\s*=\s*["\']{re.escape(prop)}["\']', tag, re.I):
            m = re.search(r'content\s*=\s*["\']([^"\']+)', tag, re.I)
            if m:
                return html.unescape(m.group(1)).strip()
    return None


def is_mp4(url):
    return bool(url and re.search(r"\.mp4(\?|$)", url, re.I))


def rss_media(entry):
    image = video = None
    for m in entry.get("media_content", []) + [
            {"url": l.get("href"), "type": l.get("type", "")}
            for l in entry.get("links", []) if l.get("rel") == "enclosure"]:
        url, typ = m.get("url"), m.get("type", "")
        if not url:
            continue
        if not video and ("video" in typ or is_mp4(url)):
            video = url
        elif not image and ("image" in typ or m.get("medium") == "image"):
            image = url
    if not image:
        for t in entry.get("media_thumbnail", []):
            image = image or t.get("url")
    return image, video


def tg_channel(name, pages=2):
    """Последние посты публичного Telegram-канала (через t.me/s/…)."""
    blocks, before = [], ""
    for _ in range(pages):
        page = requests.get(f"https://t.me/s/{name}{before}", headers=UA, timeout=25).text
        part = page.split('<div class="tgme_widget_message_wrap')[1:]
        ids = [int(x) for x in re.findall(r'data-post="[^"/]+/(\d+)"', page)]
        blocks = part + blocks
        if not ids:
            break
        before = f"?before={min(ids)}"
    posts = []
    for block in blocks:
        pid = re.search(r'data-post="([^"]+)"', block)
        text = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not pid or not text:
            continue
        when = re.search(r'<time[^>]+datetime="([^"]+)"', block)
        photo = re.search(r"(?:message_photo_wrap|link_preview_right_image|link_preview_image)"
                          r"[^>]+background-image:url\('([^']+)'\)", block)
        video = re.search(r'<video[^>]+src="([^"]+)"', block)
        body = clean(text.group(1))
        posts.append({"link": f"https://t.me/{pid.group(1)}", "title": re.sub(r"\s+", " ", body)[:220],
                      "text": body, "image": photo and photo.group(1), "video": video and video.group(1),
                      "time": datetime.fromisoformat(when.group(1)) if when else None})
    return posts


X_STATUS = re.compile(r"(?:x|twitter)\.com/(\w+)/status/(\d+)")


def x_timeline(user):
    """Последние твиты аккаунта X через страницу встраивания (бесплатно, без ключей).
    X иногда отвечает «слишком много запросов» — тогда просто пропускаем."""
    r = requests.get(f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{user}",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
    if r.status_code != 200 or not m:
        print(f"X @{user}: лента недоступна (код {r.status_code}) — пропускаю")
        return []
    entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"]["entries"]
    posts = []
    for e in entries:
        t = (e.get("content") or {}).get("tweet")
        if not t or t.get("retweeted_status"):
            continue
        text = re.sub(r"\s*https://t\.co/\w+$", "", t.get("full_text") or t.get("text") or "")
        image, video = tweet_media(t)
        posts.append({"link": f"https://x.com/{user}/status/{t['id_str']}", "title": re.sub(r"\s+", " ", text)[:220],
                      "text": text, "summary": text[:400], "image": image, "video": video, "source": None,
                      "tg": True, "time": datetime.strptime(t["created_at"], "%a %b %d %H:%M:%S %z %Y")})
    return posts


def tweet_media(t):
    """Картинка и видео (mp4 лучшего качества) из твита в формате X API."""
    image = video = None
    for m in (t.get("extended_entities") or t.get("entities") or {}).get("media", []):
        image = image or m.get("media_url_https")
        mp4 = [v for v in (m.get("video_info") or {}).get("variants", []) if v.get("content_type") == "video/mp4"]
        if mp4 and not video:
            video = max(mp4, key=lambda v: v.get("bitrate", 0))["url"]
    return image, video


def fx_tweet(user, tweet_id):
    """Один твит целиком через api.fxtwitter.com: текст, фото, видео."""
    try:
        t = requests.get(f"https://api.fxtwitter.com/{user}/status/{tweet_id}", headers=UA, timeout=20).json()["tweet"]
    except Exception as e:
        print("Твит не открылся:", tweet_id, e)
        return None
    media = t.get("media") or {}
    photo = (media.get("photos") or [{}])[0].get("url")
    video = (media.get("videos") or [{}])[0].get("url")
    return {"text": t.get("text") or "", "image": photo or (media.get("videos") or [{}])[0].get("thumbnail_url"),
            "video": video}


def feed_entries(url):
    """Записи ленты в едином виде: link, title, summary, image, video, time, source."""
    if url.startswith("x:"):
        return sorted(x_timeline(url[2:]), key=lambda p: p["time"], reverse=True)
    if url.startswith("tg:"):
        return [{**p, "summary": p["text"][:400], "source": None, "tg": True}
                for p in reversed(tg_channel(url[3:]))]   # сначала самые свежие
    feed = feedparser.parse(requests.get(url, headers=UA, timeout=25).content)
    out = []
    for e in feed.entries[:100]:
        t = e.get("published_parsed") or e.get("updated_parsed")
        image, video = rss_media(e)
        out.append({"link": e.get("link"), "title": clean(e.get("title")), "summary": clean(e.get("summary")),
                    "image": image, "video": video, "source": e.get("source", {}).get("title"),
                    "href": e.get("source", {}).get("href", ""),
                    "time": datetime(*t[:6], tzinfo=timezone.utc) if t else None})
    out.sort(key=lambda x: x["time"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def source_settings():
    """Переключатели источников (из state.json — их меняют кнопками)."""
    return {**DEFAULT_SETTINGS, **read_json("state.json", {}).get("settings", {})}


def gather(seen_path, mark=True, settings=None):
    """Новые записи из всех лент (свежее MAX_AGE_HOURS).
    mark=True — отмечает их как увиденные (облачный редактор), False — только посмотреть (панель)."""
    s = settings or source_settings()
    seen = read_json(seen_path, {"links": [], "titles": []})
    known = set(seen["links"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    items = []
    for name, url in FEEDS:
        if s.get("arab_trusted") and name in ARAB_OTHER:
            continue   # арабский мир — только из доверенных
        try:
            entries = feed_entries(url)
        except Exception as e:
            print("Лента недоступна:", name, e)
            continue
        limit = PER_FEED_OVERRIDE.get(name, PER_FEED_TRUSTED if name in TRUSTED_SOURCES else PER_FEED)
        only_reputable = name.startswith(WORLD_SEARCH) and s.get("world_reputable")
        taken = 0
        for e in entries if only_reputable else entries[:limit]:   # фильтр отсеивает многое — смотрим глубже
            if taken >= limit:
                break
            link = e["link"]
            if not link or link in known:
                continue
            if any(b in (e.get("href") or "") + link for b in BLOCKED):
                continue
            if only_reputable and not reputable(e.get("href")):
                continue   # из общих поисков — только авторитетные издания
            taken += 1
            known.add(link)
            seen["links"].append(link)
            if e["time"] and e["time"] < cutoff:
                continue
            title = e["title"]
            if "news.google.com" in link:
                title = re.sub(r"\s+-\s+[^-]+$", "", title)     # убираем « - Название сайта»
            summary = e["summary"]
            source = (e.get("source") or name) if name.startswith(WORLD_SEARCH) else name   # поиск: само издание
            items.append({**e, "time": None, "href": None, "source": source, "title": title, "id_link": link,
                          "ago": int((datetime.now(timezone.utc) - e["time"]).total_seconds() // 60) if e["time"] else None,
                          "summary": "" if summary.startswith(title[:40]) else summary[:400]})
    seen["links"] = seen["links"][-8000:]
    if mark:
        write_json(seen_path, seen)
    items = items[:400]
    for i, it in enumerate(items):
        it["index"] = i
    print(f"Новых записей в лентах: {len(items)}")
    return items, seen["titles"][-40:]


def real_link(link):
    """Google News прячет настоящую ссылку — раскодируем."""
    if "news.google.com" not in link:
        return link
    try:
        from googlenewsdecoder import gnewsdecoder
        for attempt in range(3):
            res = gnewsdecoder(link, interval=1 + attempt)
            if (res.get("status") or res.get("success")) and res.get("decoded_url"):
                return res["decoded_url"]   # разные версии библиотеки отвечают status или success
        print("Google News ссылка не раскодировалась:", res.get("message", res))
    except Exception as e:
        print("Google News ссылка не раскодировалась:", e)
    return link


def fetch_article(item):
    """Дополняет новость: настоящая ссылка, текст статьи, картинка, видео."""
    if item.get("fetched"):
        return item
    item["fetched"] = True
    m = X_STATUS.search(item["link"]) or X_STATUS.search(item.get("text") or "")
    if m and (tw := fx_tweet(*m.groups())):     # твит: берём полный текст, фото и видео
        item["text"] = tw["text"] or item.get("text", "")
        item["image"] = item.get("image") or tw["image"]
        item["video"] = item.get("video") or tw["video"]
        return item
    if item.get("tg"):          # пост из Telegram-канала — текст уже есть
        return item
    item["link"] = real_link(item["link"])
    item.setdefault("text", "")
    if "news.google.com" in item["link"]:   # не раскодировалась — на странице Google только логотип
        return item
    try:
        page = requests.get(item["link"], headers=UA, timeout=25).text
    except Exception as e:
        print("Статья не открылась:", item["link"][:100], e)
        return item
    item["text"] = (trafilatura.extract(page) or "")[:8000]
    item["page_image"] = meta(page, "og:image") or meta(page, "twitter:image")
    item["video"] = item.get("video") or next(
        (v for v in (meta(page, "og:video:secure_url"), meta(page, "og:video:url"),
                     meta(page, "og:video")) if is_mp4(v)), None)
    return item


def build_text(post, link):
    tags = [re.sub(r"[^\w]", "", t) for t in post.get("hashtags", [])]
    footer = f'<a href="{html.escape(link)}">Источник</a>'
    if CHANNEL_LINK:
        footer = f'<a href="{html.escape(CHANNEL_LINK)}">Подписаться</a> | ' + footer
    parts = [f"{post.get('emoji') or '🕌'} <b>{html.escape(post['title'])}</b>",
             html.escape(post["body"]), " ".join("#" + t for t in tags if t), footer]
    return "\n\n".join(p for p in parts if p)


BAD_IMAGE = re.compile(r"news\.google|gstatic|googleusercontent|logo|placeholder|default|favicon|"
                       r"avatar|icon|blank|spacer", re.I)
MIN_IMAGE_WIDTH = 600   # картинки меньше — размытые, не берём


def best_image(*urls):
    """Самая крупная нормальная картинка из кандидатов (не меньше MIN_IMAGE_WIDTH).
    Нет хорошей — None: тогда новость уйдёт без фото."""
    from PIL import Image
    best, best_w = None, 0
    for url in dict.fromkeys(u for u in urls if u):
        if BAD_IMAGE.search(url):
            continue
        data = download(url, 9)
        try:
            w, h = Image.open(BytesIO(data)).size if data else (0, 0)
        except Exception:
            continue
        if w >= MIN_IMAGE_WIDTH and h >= 300 and w <= 6 * h and w > best_w:
            best, best_w = url, w
    return best


def make_entry(item, post):
    """Готовый черновик для дежурного."""
    return {"text": build_text(post, item["link"]), "link": item["link"],
            "image": best_image(item.get("page_image"), item.get("image")), "video": item.get("video"),
            "tone": post.get("tone", ""), "safe": bool(post.get("safe")),
            "note": post.get("check_note", ""), "trusted": item.get("source") in TRUSTED_SOURCES,
            "importance": importance_of(post), "elite": bool(post.get("elite"))}


def importance_of(post):
    try:
        return max(1, min(5, int(post.get("importance") or 3)))
    except (TypeError, ValueError):
        return 3


# ---------------------- редактор через чат Claude ----------------------

def editor_store():
    """Папка с веткой claude/inbox: seen.json + inbox/*.json."""
    if INBOX_LOCAL or DRY_RUN:
        os.makedirs(INBOX_DIR, exist_ok=True)
        return
    if os.path.isdir(os.path.join(INBOX_DIR, ".git")):
        # не pull, а «встать ровно как на GitHub»: историю ветки раз в сутки сжимает уборка
        if git("fetch", "-q", "origin", INBOX_BRANCH, cwd=INBOX_DIR).returncode == 0:
            git("reset", "-q", "--hard", "FETCH_HEAD", cwd=INBOX_DIR)
            return
        shutil.rmtree(INBOX_DIR, ignore_errors=True)   # папка испорчена — скачаем заново
    url = git("remote", "get-url", "origin").stdout.strip()
    if not url:
        raise SystemExit("Эта папка не git-репозиторий — редактор должен работать в клоне репозитория.")
    r = git("clone", "-q", "--depth", "20", "--branch", INBOX_BRANCH, "--single-branch", url, INBOX_DIR)
    if r.returncode != 0:   # ветки ещё нет — создаём
        os.makedirs(INBOX_DIR, exist_ok=True)
        git("init", "-q", cwd=INBOX_DIR)
        git("remote", "add", "origin", url, cwd=INBOX_DIR)
        git("checkout", "-q", "-b", INBOX_BRANCH, cwd=INBOX_DIR)


def editor_push(message):
    if INBOX_LOCAL or DRY_RUN:
        return print("(DRY_RUN/INBOX_LOCAL: в git не отправляю)")
    git("add", "-A", cwd=INBOX_DIR)
    ident = [] if git("config", "user.name", cwd=INBOX_DIR).stdout.strip() else \
        ["-c", "user.name=news-editor", "-c", "user.email=news-editor@users.noreply.github.com"]
    git(*ident, "commit", "-q", "-m", message, cwd=INBOX_DIR)
    r = git("push", "-q", "origin", INBOX_BRANCH, cwd=INBOX_DIR)
    if r.returncode != 0:
        git("pull", "-q", "--rebase", "origin", INBOX_BRANCH, cwd=INBOX_DIR)
        r = git("push", "-q", "origin", INBOX_BRANCH, cwd=INBOX_DIR)
    if r.returncode != 0:   # ветку как раз сжимали — берём новую и кладём наши файлы сверху
        keep = {n: open(os.path.join(INBOX_DIR, "inbox", n), encoding="utf-8").read()
                for n in os.listdir(os.path.join(INBOX_DIR, "inbox"))}
        seen = read_json(os.path.join(INBOX_DIR, "seen.json"), {})
        git("fetch", "-q", "origin", INBOX_BRANCH, cwd=INBOX_DIR)
        git("reset", "-q", "--hard", "FETCH_HEAD", cwd=INBOX_DIR)
        for n, body in keep.items():
            with open(os.path.join(INBOX_DIR, "inbox", n), "w", encoding="utf-8") as f:
                f.write(body)
        fresh = read_json(os.path.join(INBOX_DIR, "seen.json"), {})
        for k, v in seen.items():
            if isinstance(v, list):
                fresh[k] = list(dict.fromkeys(fresh.get(k, []) + v))[-8000:]
        write_json(os.path.join(INBOX_DIR, "seen.json"), fresh)
        git("add", "-A", cwd=INBOX_DIR)
        git(*ident, "commit", "-q", "-m", message, cwd=INBOX_DIR)
        r = git("push", "-q", "origin", INBOX_BRANCH, cwd=INBOX_DIR)
    print("Отправлено в ветку", INBOX_BRANCH if r.returncode == 0 else f"— ОШИБКА: {r.stderr.strip()}")


def cmd_fetch():
    state_pull()
    editor_store()
    seen_path = os.path.join(INBOX_DIR, "seen.json")
    triage_ok = read_json("state.json", {}).get("triage_ok", 0)
    if NOW - triage_ok < 90 * 60:   # GLM работает — берём только отобранное им
        seen = read_json(seen_path, {"links": [], "titles": []})
        seen.setdefault("priority_done", [])
        done, pdone = set(seen["links"]), set(seen["priority_done"])
        short = read_json("shortlist.json", [])
        wanted = [x for x in short if x.get("priority") and x["id_link"] not in pdone]
        fresh = [x for x in short if x["id_link"] not in done and not x.get("priority") and NOW - x["t"] < 6 * 3600]
        items = wanted + fresh
        seen["links"] = (seen["links"] + [x["id_link"] for x in items])[-8000:]
        seen["priority_done"] = (seen["priority_done"] + [x["id_link"] for x in wanted])[-2000:]
        write_json(seen_path, seen)
        recent = seen["titles"][-40:]
        for i, it in enumerate(items):
            it["index"] = i
        mode = "glm"
    else:
        items, recent = gather(seen_path)
        mode = "self"
    editor_push("Отметил просмотренные новости")
    write_json("candidates.json", items)
    now = local_now()
    print(f"\nСейчас в Ташкенте: {now:%d.%m %H:%M}")
    if mode == "glm":
        print("\nОтбор уже сделал GLM. Метки:\n"
              "  ✅ — надёжный источник, факты однозначные: нужен только точный перевод и оформление;\n"
              "  🔎 — нужна перепроверка: найди подтверждение командой `python bot.py search слова`\n"
              "       (ищет по всем свежим новостям всех источников). Нет подтверждения — пропусти или safe: false.\n"
              "  ⭐ — модератор сам попросил эту новость: сделай черновик обязательно (перевод и проверка — как обычно).")
    else:
        print("\n⚠️ GLM-отбор сейчас недоступен — выбери сам из полного списка по rules.md.")
    st = load_state()
    flow = st["settings"].get("flow", "all")
    if flow == "elite":
        print("\nМодератор просит присылать ТОЛЬКО САМОЕ-САМОЕ ВАЖНОЕ (раздел «Самое-самое» в rules.md): "
              "пиши только посты с importance 5 или elite: true — остальные бот всё равно не пришлёт.")
    elif flow != "all":
        print(f"\nМодератор просит присылать {FLOW_NAMES[flow]}: посты с importance ниже {FLOW_MIN[flow]} "
              "не пиши — бот их всё равно не пришлёт.")
    taken = [t for t, _ in taken_titles(st)][-150:]
    for t in recent:
        if t not in taken:
            taken.append(t)
    print(f"\nУже брали за 3 дня ({len(taken)}) — ПОВТОРЫ НЕ БЕРИ (ту же историю другими словами тоже; "
          "бери только если есть существенно новое):")
    for t in taken:
        print("-", t)
    print(f"\nКандидаты ({len(items)}), формат: [номер] (источник) заголовок — анонс")
    for it in items:
        mark = ("⭐ " if it.get("priority") else "") + {"clear": "✅ ", "verify": "🔎 "}.get(it.get("label"), "")
        print(f"[{it['index']}] {mark}({it['source']}) {it['title']}"
              + (f" — {it['summary'][:200]}" if it["summary"] else "")
              + (f"  [GLM: {it['why']}]" if it.get("why") else ""))


def cmd_search(words):
    state_pull()
    """Поиск подтверждения: та же новость в других источниках за последние часы."""
    words = [w.lower() for w in words if len(w) > 2]
    pool = read_json("pool.json", []) + read_json("candidates.json", [])
    hits, seen = [], set()
    for x in pool:
        t = (x.get("title") or "").lower()
        score = sum(w in t for w in words)
        if score and x["link"] not in seen:
            seen.add(x["link"])
            hits.append((score, x))
    hits.sort(key=lambda h: -h[0])
    for score, x in hits[:25]:
        print(f"({x['source']}{' ✓' if x['source'] in TRUSTED_SOURCES else ''}) {x['title']}  {x['link']}")
    if not hits:
        print("Ничего похожего не нашлось.")


def cmd_article(indexes):
    items = read_json("candidates.json", [])
    out = []
    for i in indexes:
        if 0 <= i < len(items):
            it = fetch_article(items[i])
            out.append({"index": i, "source": it["source"], "title": it["title"], "link": it["link"],
                        "summary": it["summary"], "text": (it.get("text") or "")[:6000],
                        "has_photo": bool(it.get("image") or it.get("page_image")), "has_video": bool(it.get("video"))})
    write_json("candidates.json", items)   # запомнили раскодированные ссылки и картинки
    print(json.dumps(out, ensure_ascii=False, indent=1))


def cmd_send(path):
    editor_store()
    items = read_json("candidates.json", [])
    entries, links = [], []
    for post in read_json(path, []):
        i = post.get("index", -1)
        if not (0 <= i < len(items)) or not post.get("title") or not post.get("body"):
            print("Пропускаю, нет нужных полей:", post)
            continue
        entries.append({**make_entry(fetch_article(items[i]), post), "manual": bool(items[i].get("priority"))})
        links.append(items[i].get("id_link") or items[i]["link"])
    push_entries(entries, links)


def cmd_check(path):
    """Перед send: для каждого поста — похожие заголовки из взятого за 3 дня. Повторы Claude убирает сам."""
    state_pull()
    posts = read_json(path, [])
    near = similar_taken(load_state(), [p.get("title", "") for p in posts])
    clean = True
    for p, n in zip(posts, near):
        if n:
            clean = False
            print(f"\n[{p.get('index')}] {p.get('title')}")
            for v, t in n:
                print(f"   {'ТОЧНО ПОВТОР — бот не пришлёт' if v >= DUP_SURE else 'похоже'} ({v}): {t}")
    print("\nПохожих на уже взятое нет." if clean else
          "\nЕсли это то же событие без существенно новых фактов — убери пост из posts.json. "
          "Другое место, другой случай, заявление другой страны, новое развитие — оставь.")


def cmd_send_custom(path):
    """Свои новости (проверенные в панели): title, body, emoji, hashtags, link, tone, safe, check_note."""
    editor_store()
    entries = []
    for post in read_json(path, []):
        if not post.get("title") or not post.get("body") or not post.get("link"):
            print("Пропускаю, нужны title, body и link:", post)
            continue
        item = fetch_article({"link": post["link"], "image": post.get("image"), "video": post.get("video")})
        entries.append({**make_entry(item, post), "manual": True})   # своё — без фильтров
    push_entries(entries, [])


def push_entries(entries, links):
    """Кладёт черновики в ветку claude/inbox — дежурный пришлёт их в группу модерации."""
    if not entries:
        return print("Нечего отправлять.")
    folder = os.path.join(INBOX_DIR, "inbox")
    write_json(os.path.join(folder, datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".json"), entries)
    for name in os.listdir(folder):   # старые файлы (старше 3 дней) убираем
        if name[:8] < (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d"):
            os.remove(os.path.join(folder, name))
    seen_path = os.path.join(INBOX_DIR, "seen.json")
    seen = read_json(seen_path, {"links": [], "titles": []})
    seen["links"] = (seen["links"] + links)[-8000:]
    seen["titles"] = (seen["titles"] + [e["text"].split("</b>")[0].split("<b>")[-1] for e in entries])[-60:]
    write_json(seen_path, seen)
    editor_push(f"Черновики: {len(entries)}")
    print(f"Черновиков подготовлено: {len(entries)}. Дежурный пришлёт их в группу модерации в течение ~5 минут.")


def translate_titles(items):
    """Машинный перевод заголовков на русский (для панели). По источникам — в каждом один язык."""
    from concurrent.futures import ThreadPoolExecutor

    def gtranslate(text):
        r = requests.get("https://translate.googleapis.com/translate_a/single", timeout=20,
                         params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text})
        return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])

    groups = {}
    for it in items:
        if re.search(r"[а-яё]", it["title"], re.I):   # уже по-русски
            it["title_ru"] = it["title"]
        else:
            groups.setdefault(it["source"], []).append(it)

    def work(group):
        for i in range(0, len(group), 20):
            part = group[i:i + 20]
            try:
                out = gtranslate("\n".join(re.sub(r"\s+", " ", x["title"])[:230] for x in part)).split("\n")
            except Exception:
                out = []
            for x, t in zip(part, out if len(out) == len(part) else [None] * len(part)):
                x["title_ru"] = t or x["title"]

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(work, groups.values()))


def cmd_peek():
    """Для панели: свежие новости с переводом заголовков, ничего не помечая как взятое."""
    editor_store()
    items, recent = gather(os.path.join(INBOX_DIR, "seen.json"), mark=False)
    translate_titles(items)
    write_json("candidates.json", items)
    write_json("recent.json", recent)
    print(f"Кандидатов: {len(items)}")


# ---------------------- редактор через API ----------------------

SELECT_SCHEMA = {
    "type": "object",
    "properties": {"picks": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "tone": {"type": "string", "enum": ["good", "hard"]}},
        "required": ["index", "tone"], "additionalProperties": False}}},
    "required": ["picks"], "additionalProperties": False,
}

WRITE_SCHEMA = {
    "type": "object",
    "properties": {"skip": {"type": "boolean"}, "emoji": {"type": "string"}, "title": {"type": "string"},
                   "body": {"type": "string"}, "hashtags": {"type": "array", "items": {"type": "string"}},
                   "safe": {"type": "boolean"}, "check_note": {"type": "string"}},
    "required": ["skip", "emoji", "title", "body", "hashtags", "safe", "check_note"],
    "additionalProperties": False,
}


def ask_claude(client, prompt, schema, effort):
    import anthropic
    output_config = {"format": {"type": "json_schema", "schema": schema}}
    if "haiku" not in MODEL:
        output_config["effort"] = effort
    kwargs = dict(model=MODEL, max_tokens=16000, system=open("rules.md", encoding="utf-8").read(),
                  messages=[{"role": "user", "content": prompt}], output_config=output_config)
    try:
        if MODEL.startswith("claude-opus-5"):
            # Если фильтры безопасности ошибочно откажут на новость о войне —
            # запрос автоматически повторится на другой модели
            resp = client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-06-01"],
                                               fallbacks=[{"model": "claude-opus-4-8"}])
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.RateLimitError as e:
        print("Claude: лимит запросов —", e)
        return None
    except anthropic.APIStatusError as e:
        print(f"Claude: ошибка {e.status_code} —", e.message)
        return None
    except anthropic.APIConnectionError as e:
        print("Claude: нет соединения —", e)
        return None
    if resp.stop_reason in ("refusal", "max_tokens"):
        print("Claude не ответил:", resp.stop_reason)
        return None
    text = next((b.text for b in resp.content if b.type == "text"), None)
    return json.loads(text) if text else None


def collect_api(state):
    if NOW - state["last_collect"] < COLLECT_EVERY_MIN * 60 - 120:
        return
    state["last_collect"] = NOW
    import anthropic
    client = anthropic.Anthropic()

    items, recent = gather("seen.json")
    if not items:
        return
    listing = "\n".join(f"[{it['index']}] ({it['source']}) {it['title']} — {it['summary'][:200]}"
                        for it in items)
    res = ask_claude(client, f"Выбери не больше {MAX_DRAFTS_PER_COLLECT} записей по правилам отбора. "
                             f"Если подходящих нет — пустой список.\n\nНедавно уже брали:\n"
                             + ("\n".join(recent) or "(ничего)") + f"\n\nНовые записи:\n{listing}",
                     SELECT_SCHEMA, effort="low")
    titles = []
    for p in (res or {}).get("picks", [])[:MAX_DRAFTS_PER_COLLECT]:
        if not 0 <= p["index"] < len(items):
            continue
        it = fetch_article(items[p["index"]])
        post = ask_claude(client, "Напиши пост по правилам и сделай самопроверку. skip=true, если фактов "
                                  f"мало или не по теме.\n\nИсточник: {it['source']}\nЗаголовок: {it['title']}\n"
                                  f"Анонс: {it['summary']}\n\nТекст статьи:\n{it.get('text') or '(нет)'}",
                          WRITE_SCHEMA, effort="medium")
        if post and not post["skip"]:
            post["tone"] = p["tone"]
            if send_draft(state, make_entry(it, post)):
                titles.append(post["title"])
                time.sleep(2)
    seen = read_json("seen.json", {"links": [], "titles": []})
    seen["titles"] = (seen["titles"] + titles)[-60:]
    write_json("seen.json", seen)


# ---------------------- запуск ----------------------

def main():
    args = sys.argv[1:]
    if args[:1] == ["fetch"]:
        return cmd_fetch()
    if args[:1] == ["article"]:
        return cmd_article([int(a) for a in args[1:] if a.isdigit()])
    if args[:1] == ["send"]:
        return cmd_send(args[1] if len(args) > 1 else "posts.json")
    if args[:1] == ["check"]:
        return cmd_check(args[1] if len(args) > 1 else "posts.json")
    if args[:1] == ["send-custom"]:
        return cmd_send_custom(args[1] if len(args) > 1 else "custom.json")
    if args[:1] == ["peek"]:
        return cmd_peek()
    if args[:1] == ["search"]:
        return cmd_search(args[1:])

    # Без аргументов — дежурный
    if not DRY_RUN and not (BOT_TOKEN and CHANNEL_ID and MOD_CHAT_ID):
        raise SystemExit("Нужны BOT_TOKEN, CHANNEL_ID и MOD_CHAT_ID (секреты GitHub или файл .env)")
    if not DRY_RUN and pc_is_running():
        return print("Бот сейчас работает на ПК — GitHub в этот раз ничего не делает.")
    state_pull()
    state = load_state()
    try:
        process_updates(state)
        triage(state)
        ingest_inbox(state)
        if os.environ.get("ANTHROPIC_API_KEY"):
            collect_api(state)
        autopilot_and_cleanup(state)
        publish(state)
    finally:
        save_state(state)   # сохраняем даже если что-то упало посередине
        state_push()


if __name__ == "__main__":
    main()
