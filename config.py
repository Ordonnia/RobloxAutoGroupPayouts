import os
from dotenv import load_dotenv

load_dotenv()

ROBLOSECURITY = os.getenv("ROBLOSECURITY", "")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
TWOFACTOR_SECRET = os.getenv("TWOFACTOR_SECRET", "")
