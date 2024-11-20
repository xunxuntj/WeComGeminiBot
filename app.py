from flask import Flask, request
import google.generativeai as genai
import requests
import json
import os
import re
import time
from typing import Dict, Optional

app = Flask(__name__)

# 基础配置
class Config:
    # 从环境变量获取配置，如果没有则使用默认值
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
    WECHAT_WEBHOOK = os.environ.get('WECHAT_WEBHOOK')
    
    # 机器人行为配置
    ALWAYS_MENTION_SENDER = os.environ.get('ALWAYS_MENTION_SENDER', 'True').lower() == 'true'
    IGNORE_SELF_MESSAGES = os.environ.get('IGNORE_SELF_MESSAGES', 'True').lower() == 'true'
    MAX_MESSAGE_LENGTH = int(os.environ.get('MAX_MESSAGE_LENGTH', '1000'))
    
    # 速率限制配置
    RATE_LIMIT = int(os.environ.get('RATE_LIMIT', '50'))  # 每分钟最大请求数
    RATE_LIMIT_WINDOW = int(os.environ.get('RATE_LIMIT_WINDOW', '60'))  # 速率限制窗口（秒）
    
    # 响应配置
    MAX_RETRIES = int(os.environ.get('MAX_RETRIES', '3'))  # API调用最大重试次数
    RESPONSE_TIMEOUT = int(os.environ.get('RESPONSE_TIMEOUT', '30'))  # API响应超时时间（秒）
    
    # 消息模板
    RATE_LIMIT_MESSAGE = "消息太快啦，请稍后再试～"
    ERROR_MESSAGE = "抱歉，处理您的请求时出现了问题: {}"
    LENGTH_EXCEED_MESSAGE = "消息太长啦，请保持在{}字以内～"

class RateLimiter:
    def __init__(self):
        self.requests = []
    
    def can_proceed(self) -> bool:
        current_time = time.time()
        # 清理过期的请求记录
        self.requests = [req_time for req_time in self.requests 
                        if current_time - req_time < Config.RATE_LIMIT_WINDOW]
        
        if len(self.requests) < Config.RATE_LIMIT:
            self.requests.append(current_time)
            return True
        return False

class MessageHandler:
    def __init__(self):
        # 初始化Gemini
        genai.configure(api_key=Config.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-pro')
        self.rate_limiter = RateLimiter()
    
    def extract_actual_message(self, content: str) -> str:
        """提取@后的实际消息内容"""
        # 移除<@xxx>格式的提醒
        message = re.sub(r'<@.*?>', '', content)
        # 移除普通@格式的提醒
        message = re.sub(r'@[\w\-_\s]+\s+', '', message)
        # 去除首尾空格
        return message.strip()
    
    def send_to_wechat(self, message: str) -> Dict:
        """发送消息到企业微信机器人"""
        headers = {'Content-Type': 'application/json'}
        data = {
            "msgtype": "text",
            "text": {
                "content": message
            }
        }
        for _ in range(Config.MAX_RETRIES):
            try:
                response = requests.post(
                    Config.WECHAT_WEBHOOK, 
                    headers=headers, 
                    data=json.dumps(data),
                    timeout=Config.RESPONSE_TIMEOUT
                )
                return response.json()
            except requests.exceptions.RequestException as e:
                continue
        return {"error": "Failed to send message after retries"}
    
    def process_message(self, data: Dict) -> Optional[Dict]:
        """处理接收到的消息"""
        try:
            # 速率限制检查
            if not self.rate_limiter.can_proceed():
                self.send_to_wechat(Config.RATE_LIMIT_MESSAGE)
                return {"status": "rate_limited"}, 429
            
            # 检查是否是文本消息
            if not (data.get('msgtype') == 'text' and 'content' in data.get('text', {})):
                return {"status": "ignored", "message": "非文本消息"}, 200
            
            content = data['text']['content']
            
            # 检查是否需要处理（是否@机器人）
            if '@' not in content:
                return {"status": "ignored", "message": "消息中未@机器人"}, 200
            
            # 提取实际消息内容
            actual_message = self.extract_actual_message(content)
            
            # 检查消息长度
            if len(actual_message) > Config.MAX_MESSAGE_LENGTH:
                self.send_to_wechat(Config.LENGTH_EXCEED_MESSAGE.format(Config.MAX_MESSAGE_LENGTH))
                return {"status": "message_too_long"}, 400
            
            # 调用Gemini API
            response = self.model.generate_content(actual_message)
            ai_response = response.text
            
            # 处理@回复
            if Config.ALWAYS_MENTION_SENDER and 'FromUserName' in data:
                ai_response = f"@{data['FromUserName']}\n{ai_response}"
            
            # 发送响应
            self.send_to_wechat(ai_response)
            return {"status": "success"}, 200
            
        except Exception as e:
            error_message = Config.ERROR_MESSAGE.format(str(e))
            self.send_to_wechat(error_message)
            return {"status": "error", "message": str(e)}, 500

# 初始化消息处理器
handler = MessageHandler()

@app.route('/')
def home():
    return "Gemini Bot is running!"

@app.route('/webhook', methods=['POST'])
def webhook():
    """处理webhook请求"""
    return handler.process_message(request.json)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
