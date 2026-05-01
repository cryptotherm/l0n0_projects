"""
Application settings — loaded from .env or environment variables.
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # PDU Smart PDU
    PDU_HOST: str = "http://10.0.0.226"
    PDU_USER: str = "admin"
    PDU_PASSWORD: str = "admin"
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://testbench:testbench@localhost:5432/testbench"

    # OpenAI
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"

    # Brother QL printer
    PRINTER_MODEL: str = "QL-810W"
    PRINTER_IDENTIFIER: str = "tcp://192.168.1.200"  # override per site
    LABEL_TYPE: str = "62"  # 62mm continuous tape

    # Miner credentials (defaults)
    MINER_USERNAME: str = "root"
    MINER_PASSWORD: str = "root"

    # UDP discovery
    UDP_DISCOVERY_PORT: int = 14235
    UDP_BROADCAST_BIND: str = "0.0.0.0"

    # InfluxDB (optional)
    INFLUX_URL: Optional[str] = None
    INFLUX_TOKEN: Optional[str] = None
    INFLUX_ORG: Optional[str] = "cryptotherm"
    INFLUX_BUCKET: Optional[str] = "miners"

    # RGB LED strip (Raspberry Pi)
    RGB_ENABLED: bool = False
    RGB_LED_COUNT: int = 60
    RGB_LED_PIN: int = 18

    # Luxor pool
    LUXOR_USERNAME: str = ""
    LUXOR_API_KEY: str = ""
    LUXOR_POOL_URL: str = "stratum+tcp://btc.global.luxor.tech:700"
    LUXOR_POOL_URL_2: str = "stratum+tcp://btc.global.luxor.tech:700"
    LUXOR_WORKER_TEMPLATE: str = "{username}.intesting"   # default worker for unconfigured miners

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "info"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
