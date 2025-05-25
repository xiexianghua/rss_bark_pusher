import requests
import json

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
    api_url = "https://api.day.app/push"

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
    if is_archive == "1":
        payload["isArchive"] = "1"
    if level:
        payload["level"] = level

    headers = {
        "Content-Type": "application/json; charset=utf-8"
    }

    try:
        response = requests.post(api_url, data=json.dumps(payload), headers=headers)
        response_json = response.json()

        if response.status_code == 200 and response_json.get("code") == 200:
            print("Bark 通知发送成功！")
            print(f"消息 ID: {response_json.get('messageid', 'N/A')}")
            return True, response_json
        else:
            print(f"Bark 通知发送失败。")
            print(f"状态码: {response.status_code}")
            print(f"错误信息: {response_json.get('message', '未知错误')}")
            return False, response_json
    except requests.exceptions.RequestException as e:
        print(f"请求 Bark API 时发生错误: {e}")
        return False, {"error": str(e)}
    except json.JSONDecodeError:
        print(f"解析 Bark API 响应时发生错误。响应内容: {response.text}")
        return False, {"error": "JSONDecodeError", "response_text": response.text}

if __name__ == "__main__":
    # #############################################
    # ## 请将 YOUR_BARK_KEY 替换为你的真实 Key ##
    # #############################################
    my_bark_key = "YOUR_BARK_KEY" # <--- 在这里替换你的 Key

    if my_bark_key == "YOUR_BARK_KEY":
        print("错误：请在代码中替换 'YOUR_BARK_KEY' 为您真实的 Bark Key。")
        print("您可以从 Bark App 的设置中找到您的 Key，通常以 https://api.day.app/ 开头，后面跟着一串字符。")
    else:
        # 示例 1: 发送简单文本通知
        print("\n--- 示例 1: 发送简单文本通知 ---")
        success, result = send_bark_notification(
            device_key=my_bark_key,
            body="这是 Python 发送的 Bark 测试消息！"
        )
        print(f"发送结果: {'成功' if success else '失败'}")

        # 示例 2: 发送带标题和声音的通知
        print("\n--- 示例 2: 发送带标题和声音的通知 ---")
        success, result = send_bark_notification(
            device_key=my_bark_key,
            title="Python 通知",
            body="这条消息有标题，并且会播放'提示音(calypso)'铃声。",
            sound="calypso" # 您可以尝试不同的铃声，如 bell, birding, glass 等
        )
        print(f"发送结果: {'成功' if success else '失败'}")

        # 示例 3: 发送带 URL 跳转和自定义图标的通知
        print("\n--- 示例 3: 发送带 URL 跳转和自定义图标的通知 ---")
        success, result = send_bark_notification(
            device_key=my_bark_key,
            title="重要更新",
            body="点击查看详情。",
            url="https://www.python.org",
            icon="https://www.python.org/static/favicon.ico", # 图标URL必须是https
            group="Python脚本"
        )
        print(f"发送结果: {'成功' if success else '失败'}")

        # 示例 4: 发送通知并自动复制内容
        print("\n--- 示例 4: 发送通知并自动复制内容 ---")
        success, result = send_bark_notification(
            device_key=my_bark_key,
            title="验证码",
            body="您的验证码是 123456，已自动复制。",
            copy_text="123456",
            sound="gotosleep"
        )
        print(f"发送结果: {'成功' if success else '失败'}")

        # 示例 5: 发送静默通知 (直接归档，不提示)
        print("\n--- 示例 5: 发送静默通知 (直接归档，不提示) ---")
        success, result = send_bark_notification(
            device_key=my_bark_key,
            title="后台任务完成",
            body="数据备份已完成。",
            is_archive="1" # 设置为 "1" 表示直接归档
        )
        print(f"发送结果: {'成功' if success else '失败'}")

        # 示例 6: 发送指定推送级别的通知 (iOS 15+)
        print("\n--- 示例 6: 发送指定推送级别的通知 (iOS 15+) ---")
        success, result = send_bark_notification(
            device_key=my_bark_key,
            title="紧急警报",
            body="这是一个时效性通知 (Time Sensitive)。",
            level="timeSensitive" # 可选: active, timeSensitive, passive
        )
        print(f"发送结果: {'成功' if success else '失败'}")