import imaplib
import email
from email.header import decode_header
import time
import re
from openai import OpenAI
from telegram import Bot
import asyncio
from datetime import datetime, timedelta
import os
from bs4 import BeautifulSoup
import dotenv
import httpx

dotenv.load_dotenv()

GMAIL_USER = os.getenv('GMAIL_USER')

GMAIL_APP_PASSWORD = os.getenv('GMAIL_APP_PASSWORD')

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

proxy_url = os.getenv('SHADOWSOCKS_PROXY')

CHECK_INTERVAL = 10 

PROCESS_LAST_MINUTES = 10  

http_client = None

if proxy_url:
    print(f"🔐 Using proxy for OpenAI: {proxy_url}")
    http_client = httpx.Client(
        proxy=proxy_url,
        timeout=30.0
    )

client = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=http_client
)

processed_emails = set() 

def connect_to_gmail():
    """Подключение к Gmail через IMAP"""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        return mail
    except Exception as e:
        print(f"❌ Ошибка подключения к Gmail: {e}")
        return None

def parse_email_body(msg):
    """Извлечение текста из письма"""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    body = part.get_payload(decode=True).decode()
                except:
                    pass
            elif content_type == "text/html" and not body:
                try:
                    html = part.get_payload(decode=True).decode()
                    soup = BeautifulSoup(html, 'html.parser')
                    body = soup.get_text()
                except:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode()
        except:
            pass
    
    return body

def extract_task_info(email_body, subject):
    """Извлечение информации о задании из письма"""
    
    title = subject
    title = re.sub(r'до\s*\d+[\d\s]*₽', '', title).strip()
    
    description = ""
    lines = email_body.split('\n')
    for line in lines:
        line = line.strip()
        if line and 'YouDo' not in line and 'Откликнуться' not in line and 'новое задание' not in line.lower():
            if len(line) > 20:
                description += line + " "
                if len(description) > 500:
                    break
    
    budget_match = re.search(r'до\s*(\d[\d\s]*)\s*₽', subject)
    if not budget_match:
        budget_match = re.search(r'до\s*(\d[\d\s]*)\s*₽', email_body)
    
    budget = int(budget_match.group(1).replace(' ', '')) if budget_match else None
    
    return {
        'title': title.strip(),
        'description': description.strip()[:500],
        'budget': budget,
        'full_text': email_body[:1000] 
    }


def should_mention_price_in_response(email_body, subject) -> bool:
    """
    Проверяет, просит ли заказчик оценку стоимости в отклике
    Ищет ключевые фразы типа: "напиши стоимость", "указать цену", "с ценой"
    """
    price_keywords = [
        'напиши стоимость',
        'указать цену',
        'с ценой',
        'сколько стоит',
        'укажи цену',
        'цена в отклике',
        'стоимость в отклике',
        'напиши цену',
    ]
    
    full_text = (subject + ' ' + email_body).lower()
    return any(keyword in full_text for keyword in price_keywords)


def generate_response(task_info):
    """Генерация персонального отклика через GPT"""
    
    should_price = should_mention_price_in_response(task_info['full_text'], task_info['title'])
    
    price_section = ""
    if should_price and task_info['budget']:
        budget = task_info['budget']
        if budget < 1000:
            offer_price = budget - (budget * 0.1)  
        elif budget < 5000:
            offer_price = budget - (budget * 0.15)  
        else:
            offer_price = budget - (budget * 0.2)  
        
        offer_price = int(offer_price)
        if offer_price >= 1000:
            offer_price = round(offer_price / 100) * 100
        else:
            offer_price = round(offer_price / 50) * 50
        
        price_section = f"Стоимость работы: {offer_price} ₽. "
    
    task_text = (task_info['title'] + ' ' + task_info['description']).lower()
    
    if 'python' in task_text or 'питон' in task_text:
        python_advantage = "Python идеален для таких задач — быстрая разработка, чистый код и огромное количество готовых библиотек."
    else:
        python_advantage = "Возьму это на Python — язык, который позволяет реализовать проект быстро и качественно."
    
    prompt = f"""Напиши краткий, цепляющий отклик на заказ (не более 7 предложений, не маленьких).

ЗАДАНИЕ: {task_info['title']}
ОПИСАНИЕ: {task_info['description']}

ТРЕБОВАНИЯ К ОТВЕТУ:
1. Начни с "Здравствуйте"
2. В двух предложениях объясни, почему Python отличный выбор для этого конкретно (не копируй готовые ответы):
   "{python_advantage}"
3. Кратко опиши, что ты сделаешь (максимум 2 предложения, конкретно к заданию)
{f'4. Добавь цену: {price_section}, ТЫ ДОЛЖЕН САМ ОЦЕНИТЬ СЛОЖНОСТЬ И НАПИСАТЬ ЦЕНУ' if price_section else '4. НЕ указывай цену, просто скажи что готов обсудить'}
5. Закончи фразой: "Мои проекты вы можете посмотреть на GitHub @avescodder, остальные детали можем обсудить в Telegram @vsevolod_developer - я в сети 24/7"
6. Стиль: как живой человек, без шаблонов, без лишних фраз типа "Готов помочь", "Имею опыт"
7. БЕЗ markdown (**, #, списков) - просто текст

ПРИМЕР (ДЛЯ ПОНИМАНИЯ СТИЛЯ):
Здравствуйте. Разработаю для вас бота с нужной логикой — Python позволяет быстро реализовать сложные сценарии и легко интегрировать с API. Сделаю удобный интерфейс и протестирую все сценарии. Смогу реализовать проект за 12000р. Мои проекты вы можете посмотреть на GitHub @avescodder, остальные детали можем обсудить в Telegram @vsevolod_developer - я в сети 24/7.

НАПИШИ ТОЛЬКО ТЕКСТ ОТКЛИКА:"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты опытный разработчик, который пишет короткие, честные и цепляющие отклики. Без шаблонов и штампов. Язык - русский."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,  
            max_tokens=300
        )
        
        generated_text = response.choices[0].message.content.strip()
        generated_text = generated_text.strip('"').strip("'")
        
        generated_text = re.sub(r'^\d+\.\s+', '', generated_text)  
        
        return generated_text
        
    except Exception as e:
        print(f"❌ Ошибка GPT: {e}")
        return None

async def send_to_telegram(task_info, response_text):
    """Отправка в Telegram"""
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        message = f"🔔 <b>Новое задание</b>\n\n"
        message += f"📋 {task_info['title']}\n\n"
        
        if task_info['budget']:
            message += f"💰 Бюджет: {task_info['budget']:,} ₽\n\n".replace(',', ' ')
        else:
            message += "\n"
        
        message += f"<b>ОТКЛИК:</b>\n<code>{response_text}</code>"
        
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML'
        )
        
        print(f"✅ Отправлено: {task_info['title'][:50]}")
        
    except Exception as e:
        print(f"❌ Ошибка Telegram: {e}")

def get_email_date(msg):
    """Получить дату письма"""
    try:
        date_str = msg.get("Date")
        date_tuple = email.utils.parsedate_tz(date_str)
        if date_tuple:
            timestamp = email.utils.mktime_tz(date_tuple)
            return datetime.fromtimestamp(timestamp)
    except:
        pass
    return datetime.now()

def check_new_emails():
    """Проверка новых писем от YouDo"""
    mail = connect_to_gmail()
    if not mail:
        return
    
    try:
        mail.select("inbox")
        
        status, messages = mail.search(None, 'UNSEEN FROM "YouDo"')
        
        if status != "OK":
            print("❌ Ошибка поиска писем")
            return
        
        email_ids = messages[0].split()
        
        if not email_ids:
            print(f"⏳ [{datetime.now().strftime('%H:%M:%S')}] Новых писем нет")
            return
        
        time_threshold = datetime.now() - timedelta(minutes=PROCESS_LAST_MINUTES)
        
        print(f"\n📧 Найдено непрочитанных: {len(email_ids)}")
        new_count = 0
        
        for email_id in email_ids:
            try:
                email_uid = email_id.decode()
                
                if email_uid in processed_emails:
                    continue
                
                status, msg_data = mail.fetch(email_id, "(RFC822)")
                
                if status != "OK":
                    continue
                
                msg = email.message_from_bytes(msg_data[0][1])
                
                email_date = get_email_date(msg)
                if email_date < time_threshold:
                    print(f"⏭️  Пропуск старого письма от {email_date.strftime('%H:%M:%S')}")
                    processed_emails.add(email_uid)
                    continue
                
                subject = decode_header(msg["Subject"])[0][0]
                if isinstance(subject, bytes):
                    subject = subject.decode()
                
                if 'подборка' in subject.lower() or 'рекомендуем' in subject.lower():
                    print(f"⏭️  Пропуск подборки")
                    processed_emails.add(email_uid)
                    continue
                
                print(f"\n📨 {subject}")
                
                email_body = parse_email_body(msg)
                
                task_info = extract_task_info(email_body, subject)
                
                if not task_info['budget'] or task_info['budget'] < 500:
                    print(f"⏭️  Пропуск (бюджет слишком мал или не указан)")
                    processed_emails.add(email_uid)
                    continue
                
                print(f"💰 Бюджет: {task_info['budget']} ₽")
                
                response_text = generate_response(task_info)
                
                if response_text:
                    asyncio.run(send_to_telegram(task_info, response_text))
                    new_count += 1
                else:
                    print("❌ Не удалось сгенерировать отклик")
                
                processed_emails.add(email_uid)
                
            except Exception as e:
                print(f"❌ Ошибка обработки письма: {e}")
                continue
        
        if new_count > 0:
            print(f"\n✅ Обработано новых заданий: {new_count}")
        
        mail.close()
        
    except Exception as e:
        print(f"❌ Ошибка проверки почты: {e}")
    finally:
        try:
            mail.logout()
        except:
            pass

def main():
    """Основной цикл"""
    print("🚀 YouDo Email Monitor Bot запущен!")
    print(f"📧 Gmail: {GMAIL_USER}")
    print(f"⏱️  Интервал: {CHECK_INTERVAL} сек")
    print(f"🕐 Обрабатываю только письма за последние {PROCESS_LAST_MINUTES} минут")
    print(f"💬 Telegram: {TELEGRAM_CHAT_ID}")
    print("\n" + "="*50 + "\n")
    
    while True:
        try:
            check_new_emails()
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            print("\n\n👋 Бот остановлен")
            break
        except Exception as e:
            print(f"❌ Критическая ошибка: {e}")
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()