# Планерный бот (Джарвис)

Личный ассистент в Telegram на DeepSeek:
- в `MORNING_TIME` присылает идею из книги, как применить её сегодня, и мотивацию под ваши цели;
- в `EVENING_TIME` продолжение мысли и вопрос о прошедшем дне, в `NECK_TIME` напоминание про шею;
- отвечает на текст, голосовые (Yandex SpeechKit) и скриншоты (Yandex Vision OCR), помнит последние 12 реплик;
- ищет в интернете через Yandex Search API и читает страницы по ссылке;
- видит планер ialexrd.github.io/planer (Firestore проекта `planer-da5eb`, документ `users/{uid}`): «что у меня завтра?», «добавь на пятницу в 15:00 созвон», «отметь пост сделанным»; утром присылает план дня, вечером спрашивает про неотмеченные дела;
- команды: /idea, /evening, /books, /reset, /id;
- отвечает только владельцу (`OWNER_ID`).

Работает на том же сервере, что и `assistant-bot`: своя папка `/opt/planner-bot`, своя служба `planner-bot`,
свой токен Telegram. Туннель к Telegram общий, поэтому обязательно `TELEGRAM_POLL_TIMEOUT=2`.

Личные файлы `context.md` (о себе) и `books.txt` (список книг) в репозиторий не входят и живут только на сервере.
Образцы: `context.example.md`, `books.example.txt`. Править их можно без перезапуска.

## Обновление на сервере
```bash
cd /opt/planner-bot && git pull && .venv/bin/pip install -q -r requirements.txt && systemctl restart planner-bot
```

## Установка с нуля
```bash
git clone https://github.com/ialexrd/planner-bot /opt/planner-bot && cd /opt/planner-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env
cp context.example.md context.md && cp books.example.txt books.txt
cp planner-bot.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now planner-bot
```

## Полезное
```bash
journalctl -u planner-bot -f                 # логи
systemctl is-active planner-bot assistant-bot
```
