import qrcode
from io import BytesIO

def create_qr_code(data: str) -> BytesIO:
    """
    Создает QR-код из строки и возвращает его в виде байтового потока в памяти.
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    # Цвета бренд-бука FLASK: глубокий синий на тёплой бумаге.
    # В оттенках серого разница ~157/255 — сканеры читают уверенно.
    img = qr.make_image(fill_color="#2457C5", back_color="#F7F1E8")
    
    # Сохраняем изображение в байтовый поток в памяти
    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0) # Перемещаем курсор в начало файла
    
    return bio