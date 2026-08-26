# Steelon Exchange Bot

Бот стежить за курсом EUR у каналах обмінників, збирає його в одну таблицю
й показує її в Telegram. Працює у двох частинах:

- **оновлювач курсів** — щогодини з 09:00 до 18:00 за Києвом сканує канали,
  оновлює таблицю в Steelon і розсилає її підписникам;
- **інтерактивний бот** — кнопки в приватному чаті після `/start`.

Обидві частини запускає одна команда:

```text
python telegram_bot.py --with-rate-updater
```

## Кнопки

Після `/start` у приватному чаті:

| Кнопка | Що робить |
| --- | --- |
| 📩 Надіслати мені курс | надсилає останню збережену таблицю особисто |
| 📣 Надіслати курс у Steelon | публікує таблицю в канал/групу Steelon |
| 🕒 Останні оновлення | таблиця + час останнього оновлення за Києвом |
| 🔔 Отримувати щодня | вмикає персональну щоденну розсилку |
| 🔕 Вимкнути щоденні | вимикає її |

Публікація в Steelon доступна лише тим, чий числовий Telegram ID вказано в
`TELEGRAM_ADMIN_IDS`. Усі інші отримують відмову — це навмисно перевіряється
за ID користувача, який натиснув кнопку, а не за чатом.

Підписник отримує рівно одну таблицю на день — після першого успішного
оновлення курсу. Якщо користувач заблокував бота, його автоматично прибирає
зі списку розсилки.

## Змінні середовища

| Змінна | Обовʼязкова | Призначення |
| --- | --- | --- |
| `BOT_TOKEN` | так | токен від @BotFather |
| `STEELON_CHAT_ID` | так | `@username` каналу або числовий ID групи |
| `TELEGRAM_ADMIN_IDS` | так | числові ID адміністраторів через кому |
| `DATA_DIR` | ні | де зберігати дані, у Docker — `/app/data` |
| `AUTO_POST_STEELON` | ні | `0` вимикає автопублікацію в канал |

Зразок — у `.env.example`. Секрети не зберігаються в репозиторії: `.env`
внесений і в `.gitignore`, і в `.dockerignore`, тож не потрапляє ані в git,
ані всередину образу.

## Запуск на Oracle Cloud Always Free

Підійде будь-яка Always Free інстанція — і VM.Standard.E2.1.Micro (x86),
і Ampere A1 (ARM). Образ збирається під обидві архітектури.

```bash
# 1. Docker на Oracle Linux
sudo dnf install -y docker docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker

# 2. Код
git clone https://github.com/lukyniukmykola-create/steelon-exchange.git
cd steelon-exchange

# 3. Налаштування
cp .env.example .env
nano .env          # BOT_TOKEN, STEELON_CHAT_ID, TELEGRAM_ADMIN_IDS

# 4. Старт
docker compose up -d --build
docker compose logs -f
```

Вихідні зʼєднання до `api.telegram.org`, `t.me` і `bank.gov.ua` мають бути
дозволені. На Oracle Linux вихідний трафік типово відкритий, але якщо
зʼєднання не проходять — перевірте `iptables`, який Oracle ставить за
замовчуванням.

Дані лежать у папці `data/` поруч із проєктом і переживають перезбирання
контейнера. При першому старті, поки власної історії ще немає, бот бере
таблицю з `rates_history.json` у репозиторії, щоб кнопки працювали одразу.

## Важливо: не запускайте дві публікації одночасно

У репозиторії лишився GitHub Actions workflow **Daily currency rates to
Telegram**, який теж публікує курс у канал. Якщо підняти контейнер і не
вимкнути workflow, у каналі будуть дублікати.

Виберіть одне:

- **контейнер публікує** — вимкніть workflow: Actions → Daily currency rates
  to Telegram → `···` → Disable workflow;
- **публікує GitHub Actions** — залиште workflow, а контейнеру поставте
  `AUTO_POST_STEELON=0`. Тоді бот лишається інтерактивним і розсилає
  підписникам, але в канал сам не пише.

## Локальний запуск без Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python telegram_bot.py --with-rate-updater
```

Бот працює через long polling, тому webhook у @BotFather має бути вимкнений.
Одночасно може опитувати Telegram лише один процес — два запущені боти з тим
самим токеном будуть конфліктувати.

## Корисні прапорці

```bash
python fetch_and_post.py --dry-run    # показати таблицю, нікуди не надсилати
python fetch_and_post.py --no-wait    # оновити зараз, не чекаючи рівної години
python fetch_and_post.py --force      # надіслати нову таблицю навіть без змін
```
