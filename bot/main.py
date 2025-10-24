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
    """Извлечение текста из письма - специализировано для YouDo"""
    body = ""
    
    try:
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                
                if "attachment" in content_disposition:
                    continue
                
                if content_type == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            for encoding in ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']:
                                try:
                                    body = payload.decode(encoding)
                                    if body.strip() and len(body) > 20:
                                        return body.strip()
                                except:
                                    continue
                    except Exception as e:
                        pass
                
                if content_type == "text/html":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            for encoding in ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']:
                                try:
                                    html = payload.decode(encoding)
                                    soup = BeautifulSoup(html, 'html.parser')
                                    
                                    for tag in soup(['script', 'style', 'meta', 'link', 'noscript']):
                                        tag.decompose()
                                    
                                    text = soup.get_text(separator='\n', strip=True)
                                    
                                    text = ''.join(c for c in text if not ((0x200b <= ord(c) <= 0x200f) or (0x2060 <= ord(c) <= 0x2064) or ord(c) == 0xfeff or ord(c) == 0xad))
                                    
                                    lines = [line.strip() for line in text.split('\n') if line.strip() and len(line.strip()) > 1]
                                    cleaned_text = '\n'.join(lines)
                                    
                                    if cleaned_text:
                                        body = cleaned_text
                                        break
                                except:
                                    continue
                        
                        if body:
                            return body.strip()
                    except Exception as e:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    for encoding in ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']:
                        try:
                            body = payload.decode(encoding)
                            if body.strip() and len(body) > 20:
                                return body.strip()
                        except:
                            continue
            except Exception as e:
                pass
        
        if body:
            return body.strip()
        else:
            print(f"⚠️  Текст не найден! Структура: {[part.get_content_type() for part in msg.walk()]}")
            return ""
        
    except Exception as e:
        print(f"❌ Ошибка в parse_email_body: {e}")
        return ""

def extract_task_info(email_body, subject):
    """Извлечение информации о задании из письма"""
    
    clean_text = lambda t: ''.join(c for c in t if not ((0x200b <= ord(c) <= 0x200f) or (0x2060 <= ord(c) <= 0x2064) or ord(c) == 0xfeff or ord(c) == 0xad))
    
    email_body = clean_text(email_body)
    subject = clean_text(subject)
    
    title = subject
    title = re.sub(r'до\s*\d+[\d\s]*₽', '', title).strip()
    
    description = ""
    lines = email_body.split('\n')
    
    skip_keywords = ['YouDo', 'Откликнуться', 'новое задание', 'подборка', 'рекомендуем', 'письма', 'gmail']
    
    for line in lines:
        line_stripped = line.strip()
        
        if not line_stripped:
            continue
        
        if any(keyword.lower() in line.lower() for keyword in skip_keywords):
            continue
        
        description += line_stripped + " "
    
    budget_match = re.search(r'до\s*(\d[\d\s]*)\s*₽', subject)
    if not budget_match:
        budget_match = re.search(r'до\s*(\d[\d\s]*)\s*₽', email_body)
    
    budget = int(budget_match.group(1).replace(' ', '')) if budget_match else None
    
    description = ' '.join(description.split())
    
    description_for_log = ''.join(c for c in description if ord(c) >= 32 or c in '\n\t\r')
    
    return {
        'title': title.strip(),
        'description': description.strip(),
        'budget': budget,
        'full_text': email_body
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
    
    # task_text = (task_info['title'] + ' ' + task_info['description']).lower()

    
    prompt = f"""
        Ты — профессиональный Python-разработчик, который пишет короткие, деловые и уверенные отклики на заказы на YouDo. 
        Твоя цель — показать компетентность и серьёзный подход, не расписывая лишнего. 
        Пиши как специалист, который берёт задачу и делает её — спокойно, без лишних эмоций.

        Информация о заказе:
        ЗАДАНИЕ: {task_info['title']}
        ОПИСАНИЕ: {task_info['description']}

        Правила написания отклика:
        1. Начни с "Добрый день!".
        2. Сразу по делу — 1 предложение, где ты показываешь, что понял задачу и готов выполнить её в полном объёме.
        3. 1–2 предложения — чётко опиши, **что именно ты сделаешь и как**, без технических деталей, но с ощущением уверенности и контроля.
        Можно добавить конкретную фразу о похожем опыте или подходе ("реализовывал аналогичные решения", "настраивал подобные процессы", "знаю, как оптимизировать под задачу").
        {f'4. Если уместно, укажи примерную цену и срок, ориентируясь на сложность: {price_section}' if price_section else '4. Без цены — просто укажи, что детали и бюджет можно обсудить в Telegram.'}
        5. Заверши стандартно:
        “Портфолио — GitHub @avescodder. Связь — Telegram @vsevolod_developer.”
        6. Не используй markdown, списки, кавычки, восклицательные знаки и фразы вроде “качественно”, “быстро”, “большой опыт”.
        7. Стиль — деловой, уверенный, лаконичный. Без эмоций, без фальши. Каждое слово по делу.
        8. Отклик должен звучать как реальное коммерческое предложение от специалиста уровня middle/senior, а не шаблон.

        Выведи только сам отклик, без пояснений.
        """

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
            return
        
        time_threshold = datetime.now() - timedelta(minutes=PROCESS_LAST_MINUTES)
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
                    processed_emails.add(email_uid)
                    continue
                
                subject = decode_header(msg["Subject"])[0][0]
                if isinstance(subject, bytes):
                    subject = subject.decode()
                
                if 'подборка' in subject.lower() or 'рекомендуем' in subject.lower():
                    processed_emails.add(email_uid)
                    continue
                
                
                email_body = parse_email_body(msg)
                
                task_info = extract_task_info(email_body, subject)
                
                if not task_info['budget'] or task_info['budget'] < 500:
                    processed_emails.add(email_uid)
                    continue
                
                
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