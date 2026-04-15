import logging
import requests
from config import settings

logger = logging.getLogger(__name__)

def send_push_notification(
    target_user_ids: list[str],
    heading: str,
    content: str,
    data: dict | None = None
) -> bool:
    """
    發送 OneSignal 推播通知
    :param target_user_ids: 接收者的 user_id 列表 (對應 OneSignal 的 external_id)
    :param heading: 推播標題
    :param content: 推播內容
    :param data: 夾帶的隱藏資料 (例如跳轉路徑)
    """
    if not settings.onesignal_app_id or not settings.onesignal_rest_api_key:
        # 開發環境如果沒設 Key，只印 Log 不報錯
        logger.warning("OneSignal 尚未配置 (缺 App ID 或 API Key)，略過推播發送")
        return False

    if not target_user_ids:
        return False

    url = "https://onesignal.com/api/v1/notifications"
    headers = {
        "Authorization": f"Basic {settings.onesignal_rest_api_key}",
        "Content-Type": "application/json; charset=utf-8",
    }
    
    # 建構 payload
    payload = {
        "app_id": settings.onesignal_app_id,
        "include_aliases": {"external_id": target_user_ids}, # 指定 user_id
        "target_channel": "push",
        "headings": {"en": heading, "zh-Hant": heading},
        "contents": {"en": content, "zh-Hant": content},
    }
    
    if data:
        payload["data"] = data

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=5)
        response.raise_for_status()
        logger.info(f"OneSignal 推播發送成功: {response.text}")
        return True
    except Exception as e:
        logger.error(f"OneSignal 推播發送失敗: {str(e)}")
        return False