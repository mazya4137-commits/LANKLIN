"""Local, configurable server-side word masking for LANLink."""
from pathlib import Path
import json
import os
import re
import threading
import unicodedata

DEFAULT_TERMS = [
    'бляд*', 'пизд*', 'хуй*', 'хуе*', 'ебан*', 'ебат*', 'ёбан*', 'ебл*',
    'сука', 'мудак*', 'долбоеб*', 'идиот*', 'дебил*', 'дурак*', 'тупиц*',
    'ублюд*', 'тварь', 'акмак', 'келесоо', 'fuck*', 'shit*', 'bitch*', 'idiot*',
]
LOCK=threading.RLock()
TOKEN=re.compile(r'[^\W_]+(?:[._-][^\W_]+)*',re.UNICODE)
LOOKALIKE=str.maketrans({'0':'о','3':'з','4':'а','@':'а','a':'а','e':'е','o':'о','c':'с','p':'р','x':'х','y':'у','k':'к'})


def normalize(word):
    word=unicodedata.normalize('NFKC',word).lower().replace('ё','е')
    return ''.join(ch for ch in word.translate(LOOKALIKE) if ch.isalnum())


def validate(term):
    if not isinstance(term,str):
        raise ValueError('Введите слово')
    value=unicodedata.normalize('NFKC',term.strip().lower())
    root=value.endswith('*')
    body=value[:-1] if root else value
    if not 3<=len(body)<=32 or not all(ch.isalnum() for ch in body):
        raise ValueError('Одно слово от 3 до 32 букв или цифр; * в конце означает начало слова')
    return body+('*' if root else '')


def path(data):
    return Path(data)/'censorship.json'


def initial(data):
    p=path(data)
    if not p.exists():
        p.write_text(json.dumps({'terms':DEFAULT_TERMS},ensure_ascii=False,indent=2),encoding='utf-8')


def terms(data):
    with LOCK:
        payload=json.loads(path(data).read_text(encoding='utf-8'))
        if not isinstance(payload,dict) or not isinstance(payload.get('terms'),list):
            raise ValueError('Некорректный файл censorship.json')
        return [validate(x) for x in payload['terms']]


def update(data,word,remove=False):
    word=validate(word)
    with LOCK:
        current=terms(data)
        if remove:
            if word not in current:
                raise ValueError('Слово не найдено')
            current.remove(word)
        else:
            if word in current:
                raise ValueError('Слово уже есть в списке')
            if len(current)>=300:
                raise ValueError('Максимум 300 слов')
            current.append(word)
        dest=path(data)
        tmp=dest.with_suffix('.tmp')
        try:
            tmp.write_text(json.dumps({'terms':current},ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(tmp,dest)
        finally:
            tmp.unlink(missing_ok=True)
        return current


def censor(value,data):
    rules=[]
    for item in terms(data):
        root=item.endswith('*')
        needle=normalize(item[:-1] if root else item)
        rules.append((needle,root))
    def mask(match):
        token=match.group()
        normal=normalize(token)
        if any(normal.startswith(word) if root else normal==word for word,root in rules):
            return '*'*len(token)
        return token
    result=TOKEN.sub(mask,value)
    return result,result!=value
