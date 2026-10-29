from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./data/crm.db"   # прод: postgresql+psycopg://crm:pass@db/crm
    secret_key: str = "change-me"
    admin_password: str = "change-me"              # пароль власника при першому запуску
    owner_login: str = "vadim"                     # логін власника при першому запуску
    owner_name: str = "Вадим Бондар"
    work_hour_from: int = 10                       # робочі години клініки (вільні вікна в календарі)
    work_hour_to: int = 20
    public_url: str = "http://localhost:8000"      # для посилань у ТГ-нагадуваннях
    files_dir: str = "./data/files"
    timezone: str = "Europe/Kyiv"

    # CleverBox REST API v3
    cleverbox_token: str = ""
    cleverbox_base_url: str = "https://cbox.mobi/api/v3/rest"
    cleverbox_filial_id: int | None = None
    cleverbox_min_interval_sec: float = 1.2        # ліміт API: не більше 1 запиту/сек

    # Checkbox (ПРРО) — оборот ФОП для «Звіти → Ліміти ФОП». Кабінет на кожен ФОП: логін/пароль касира
    # (Checkbox → Касири), лише читання Z-звітів. Секрети — тільки в .env.
    checkbox_base_url: str = "https://api.checkbox.in.ua/api/v1"
    checkbox_1_name: str = "Колган"
    checkbox_1_login: str = ""
    checkbox_1_password: str = ""
    checkbox_2_name: str = "Бондар"
    checkbox_2_login: str = ""
    checkbox_2_password: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""               # чат/група адмінів для нагадувань
    telegram_allowed_chat_ids: str = ""            # з яких чатів бот приймає файли (через кому)

    # Google Sheets (service account)
    google_service_account_file: str = ""
    google_sheet_id: str = ""

    # Логіка нагадувань
    remind_not_booked_after_days: int = 2          # план без запису довше N днів -> нагадати
    remind_care_days_before_end: int = 10          # за скільки днів до закінчення догляду
    remind_hour: int = 10                          # о котрій слати дайджест
    default_procedure_interval_days: int = 28
    sync_lookback_days: int = 120

    # Технічні сповіщення (збої синку/бекапу) — якщо порожньо, власникам з chat ID у «Мій кабінет»
    telegram_alert_chat_id: str = ""
    alert_repeat_hours: int = 6

    # Резервні копії
    backup_dir: str = "./backups"                 # локальна папка (у Docker — окремий том поза data/)
    backup_copy_dir: str = ""                      # друга копія: змонтований другий диск / хмара (rclone mount)
    backup_keep: int = 14                          # скільки останніх копій зберігати
    backup_hour: int = 3                           # о котрій щодня (за TIMEZONE)
    backup_with_files: bool = True                 # включати фото і PDF
    backup_password: str = ""                      # шифрування копії, що йде в Telegram (без пароля — не надсилаємо)
    backup_telegram: bool = True                   # слати зашифровану копію власнику в Telegram (до 49 МБ)

    # Захист входу
    login_max_attempts: int = 5
    login_block_minutes: int = 15

    # Бот для пацієнтів (OpenClaw) — CRM надсилає через нього повідомлення пацієнтам
    patient_bot_token: str = ""
    patient_bot_username: str = ""                 # для посилання t.me/<username>?start=...

    # TurboSMS (Viber з резервом SMS)
    turbosms_token: str = ""
    turbosms_sender_sms: str = ""                  # зареєстроване альфа-імʼя для SMS
    turbosms_sender_viber: str = ""                # альфа-імʼя Viber (якщо підключено)
    turbosms_viber_ttl: int = 3600                 # скільки чекати доставки Viber перед SMS, сек
    sms_price: float = 1.0                         # оцінка вартості SMS-сегмента, грн (для превʼю розсилки)
    viber_price: float = 0.7                       # оцінка вартості Viber-повідомлення, грн

    # Правила розсилок
    quiet_hours_start: int = 20                    # не слати маркетинг з 20:00…
    quiet_hours_end: int = 9                       # …до 09:00
    marketing_frequency_days: int = 14             # не частіше ніж раз на N днів одному пацієнту
    marketing_footer: str = "Відписатися — STOP"   # додається до маркетингових SMS/Viber

    @property
    def allowed_chats(self) -> set[str]:
        return {c.strip() for c in self.telegram_allowed_chat_ids.split(",") if c.strip()}


settings = Settings()
