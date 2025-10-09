import paho.mqtt.client as mqtt
import logging

logger = logging.getLogger(__name__)

def send_to_mqtt(config, title, content):
    """发送消息到 MQTT 主题"""
    if not config or not config.get('enabled'):
        return

    host = config.get('host')
    port = config.get('port')
    topic = config.get('topic')

    if not all([host, port, topic]):
        logger.warning("MQTT 配置不完整，跳过发送。")
        return

    try:
        client = mqtt.Client()
        if config.get('username') and config.get('password'):
            client.username_pw_set(config['username'], config['password'])
        
        client.connect(host, port, 60)
        
        payload = f"{title}\n\n{content}"
        
        result = client.publish(topic, payload=payload.encode('utf-8'))
        
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"成功将消息发送到 MQTT 主题 '{topic}' (MID: {result.mid})。")
        else:
            logger.error(f"发送消息到 MQTT 主题 '{topic}' 失败，错误码: {result.rc}")

        client.disconnect()

    except Exception as e:
        logger.error(f"连接或发送到 MQTT 服务器 {host}:{port} 时出错: {e}", exc_info=True)
