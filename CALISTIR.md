# Nasıl çalıştırılır (Windows)

Bu rehber demoyu kendi bilgisayarında, Stellar testnet üzerinde çalıştırman için. Testnet sahte para kullanır; hiçbir şey kaybetmezsin.

## 1. Python kurulu mu?

PowerShell'i aç ve yaz:

```powershell
py --version
```

`Python 3.10` veya üstü yazıyorsa devam et. Yazmıyorsa python.org'dan Python'u kur (kurulumda **"Add python.exe to PATH"** kutusunu işaretle).

## 2. Kurulum (bir kere)

```powershell
cd C:\stellar\x402-stellar
py -m venv .venv
.venv\Scripts\activate
pip install -e ".[demo,dev]"
```

## 3. Testleri çalıştır (internet gerekmez)

```powershell
pytest
```

`40 passed` görmelisin.

## 4. Testnet demosu

```powershell
python examples\demo.py
```

Ne olur:

1. İlk çalıştırmada 3 testnet hesabı açılır ve Friendbot hepsine sahte XLM yükler. Hesaplar `examples\testnet_accounts.json` dosyasına kaydedilir. **Bu dosyayı GitHub'a yükleme** (`.gitignore` zaten engelliyor).
2. `http://127.0.0.1:8402/weather` adresinde ücretli bir API açılır (her çağrı 0.1 XLM).
3. "Ajan" API'yi çağırır, 402 alır, ödemeyi imzalar, tekrar dener ve veriyi alır.
4. Ekrana yanıt, işlemin stellar.expert linki ve bakiyeler basılır.

Linke tıklayınca işlemi Stellar'ın kendi blok gezgininde görürsün. Bu, Instaward için en güçlü kanıtın: ekran kaydını al.

## Bir şey ters giderse

Bu kod bulutta, ağa bağlanmadan, Stellar'ı taklit eden bir test ağıyla doğrulandı. Gerçek testnet'te ilk kez çalışacak. Hata alırsan çıktının tamamını kopyalayıp bana gönder, düzeltelim.
