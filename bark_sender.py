import requests
import json
import logging # 添加日志记录
import os

logger = logging.getLogger(__name__) # 获取 logger 实例

def send_bark_notification(device_key, body, title="", sound="", icon="", group="", url="", copy_text="", is_archive="0", level=""):
    """
    发送 Bark 推送通知。

    参数:
    device_key (str): 你的 Bark 设备 Key。
    body (str): 推送通知的主要内容。 (必填)
    title (str, optional): 推送通知的标题。
    sound (str, optional): 推送铃声。例如 "bell", "birdsong", "glass"。更多请参考 Bark App。
    icon (str, optional): 自定义推送图标的 URL (必须是 https)。
    group (str, optional): 推送消息分组。
    url (str, optional): 点击推送时跳转的 URL。
    copy_text (str, optional): 点击推送时自动复制的文本。
    is_archive (str, optional): 设置为 "1" 时，推送会直接存为历史记录，而不会弹出提示。
    level (str, optional): 推送级别 (iOS 15+)，可选 "active", "timeSensitive", "passive"。
    """
    api_url = "https://api.day.app/push" # 默认官方API

    # 允许通过环境变量覆盖 Bark API 地址
    bark_api_server = os.environ.get('BARK_API_SERVER', '').strip()
    if bark_api_server:
        if bark_api_server.endswith('/'): # 移除末尾的 /
            bark_api_server = bark_api_server[:-1]
        api_url = f"{bark_api_server}/push"
        logger.info(f"使用自定义 Bark API 服务器: {api_url}")


    payload = {
        "device_key": device_key,
        "body": body
    }

    if title:
        payload["title"] = title
    if sound:
        payload["sound"] = sound
    if icon:
        payload["icon"] = icon
    if group:
        payload["group"] = group
    if url:
        payload["url"] = url
    if copy_text:
        payload["copy"] = copy_text # API 参数名为 'copy'
    if is_archive == "1": # API期望的是字符串 "1"
        payload["isArchive"] = "1"
    if level:
        payload["level"] = level

    headers = {
        "Content-Type": "application/json; charset=utf-8"
    }
    
    # 记录将要发送的payload (不记录device_key)
    log_payload = payload.copy()
    if 'device_key' in log_payload:
        log_payload['device_key'] = '***REDACTED***'
    logger.debug(f"发送 Bark 请求到 {api_url}，Payload: {log_payload}")

    try:
        response = requests.post(api_url, data=json.dumps(payload), headers=headers, timeout=10) # 添加超时
        response_json = response.json()

        if response.status_code == 200 and response_json.get("code") == 200:
            logger.info(f"Bark 通知发送成功！消息 ID: {response_json.get('messageid', 'N/A')}, Title: {title[:30]}")
            return True, response_json
        else:
            logger.error(f"Bark 通知发送失败。状态码: {response.status_code}, 错误: {response_json.get('message', '未知错误')}, Title: {title[:30]}")
            return False, response_json
    except requests.exceptions.Timeout:
        logger.error(f"请求 Bark API ({api_url}) 超时。Title: {title[:30]}")
        return False, {"error": "Request Timeout", "message": f"请求 Bark API ({api_url}) 超时"}
    except requests.exceptions.RequestException as e:
        logger.error(f"请求 Bark API ({api_url}) 时发生错误: {e}, Title: {title[:30]}")
        return False, {"error": str(e)}
    except json.JSONDecodeError:
        logger.error(f"解析 Bark API ({api_url}) 响应时发生错误。响应内容: {response.text}, Title: {title[:30]}")
        return False, {"error": "JSONDecodeError", "response_text": response.text}

if __name__ == "__main__":
    # 配置基本日志以便在直接运行时看到 bark_sender 的日志输出
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        handlers=[logging.StreamHandler()]
    )
    logger.info("直接运行 bark_sender.py 进行测试...")
    
    # #############################################
    # ## 请将 YOUR_BARK_KEY 替换为你的真实 Key ##
    # #############################################
    my_bark_key = os.environ.get("BARK_DEVICE_KEY", "YOUR_BARK_KEY") # 尝试从环境变量获取

    if my_bark_key == "YOUR_BARK_KEY":
        logger.error("错误：请在代码中替换 'YOUR_BARK_KEY' 为您真实的 Bark Key，或设置 BARK_DEVICE_KEY 环境变量。")
        logger.info("您可以从 Bark App 的设置中找到您的 Key，通常以 https://api.day.app/ 开头，后面跟着一串字符。")
    else:
        logger.info(f"使用 Bark Key: ...{my_bark_key[-4:]}") # 仅显示末尾几位

        # 示例 1: 发送简单文本通知
        logger.info("\n--- 示例 1: 发送简单文本通知 ---")
        success, result = send_bark_notification(
            device_key=my_bark_key,
            body="这是 Python 发送的 Bark 测试消息！"
        )
        logger.info(f"发送结果: {'成功' if success else '失败'}, 响应: {result}")

        # 示例 2: 发送带标题和声音的通知
        logger.info("\n--- 示例 2: 发送带标题和声音的通知 ---")
        success, result = send_bark_notification(
            device_key=my_bark_key,
            title="Python 通知",
            body="这条消息有标题，并且会播放'提示音(calypso)'铃声。",
            sound="calypso" # 您可以尝试不同的铃声，如 bell, birding, glass 等
        )
        logger.info(f"发送结果: {'成功' if success else '失败'}, 响应: {result}")

        # 示例 3: 发送带 URL 跳转和自定义图标的通知
        logger.info("\n--- 示例 3: 发送带 URL 跳转和自定义图标的通知 ---")
        success, result = send_bark_notification(
            device_key=my_bark_key,
            title="重要更新",
            body="点击查看详情。",
            url="https://www.python.org",
            icon="https://www.python.org/static/favicon.ico", # 图标URL必须是https
            group="Python脚本"
        )
        logger.info(f"发送结果: {'成功' if success else '失败'}, 响应: {result}")