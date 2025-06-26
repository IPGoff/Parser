#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import hashlib
import logging
import argparse
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests
import pandas as pd

# ====== Логирование ======
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class Config:
    SECRET: str = '361132d17590a57a9dcce90d1368f5e0'
    APP_VERSION: str = '6.43.0'
    DEVICE_ID: str = 'A-d9b391de-a21b-4e1a-b980-abf75675e788'
    PLATFORM: str = 'omniapp'
    BRAND: str = 'lo'
    CLIENT: str = 'android_11_6.43.0'
    DELIVERY_MODE: str = 'pickup'
    API_HOST: str = 'https://api.lenta.com'
    RPC_HOST: str = 'https://lentochka.lenta.com'


class LentaApiClient:
    def __init__(self, pickup_store: int, category_id: int):
        self.pickup_store = pickup_store
        self.category_id = category_id
        self.session = requests.Session()
        self.session_token: Optional[str] = None

    def _make_headers(self, path: str, host: str) -> Dict[str, str]:
        ts = str(int(time.time()))
        base_url = host + path.split('?')[0]
        token_source = Config.SECRET + base_url + ts
        qtoken = hashlib.md5(token_source.encode('utf-8')).hexdigest()

        localtime = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        headers = {
            'App-Version': Config.APP_VERSION,
            'Timestamp': ts,
            'Qrator-Token': qtoken,
            'Sessiontoken': self.session_token or '',
            'Deviceid': Config.DEVICE_ID,
            'X-Device-Id': Config.DEVICE_ID,
            'X-Device-Brand': 'Google',
            'X-Device-Name': 'Genymobile',
            'X-Device-Os': 'Android',
            'X-Device-Os-Version': '30',
            'Advertisingid': '',
            'X-Organisation-Id': '',
            'X-Platform': Config.PLATFORM,
            'X-Retail-Brand': Config.BRAND,
            'Localtime': localtime,
            'Client': Config.CLIENT,
            'User-Agent': 'okhttp/4.9.1',
            'Content-Type': 'application/json; charset=utf-8',
            'X-Delivery-Mode': Config.DELIVERY_MODE,
        }
        return headers

    def init_session(self) -> None:
        # 1) Получаем гостевой sessionId
        path = '/v1/auth/session/guest/token'
        url = Config.API_HOST + path
        r = self.session.get(url, headers=self._make_headers(path, Config.API_HOST))
        r.raise_for_status()
        self.session_token = r.json()['sessionId']
        logger.info("Session token obtained: %s", self.session_token)

        # 2) JSON-RPC для WAF cookie
        rpc_path = '/jrpc/deliveryModeGet'
        rpc_url = Config.RPC_HOST + rpc_path
        payload = {"jsonrpc": "2.0", "method": "deliveryModeGet", "id": int(time.time() * 1000)}
        r = self.session.post(rpc_url,
                              headers=self._make_headers(rpc_path, Config.RPC_HOST),
                              json=payload)
        r.raise_for_status()
        # переносим WAF-куку на главный домен
        waf = self.session.cookies.get('Utk_SssTkn', domain='lentochka.lenta.com')
        if waf:
            self.session.cookies.set('Utk_SssTkn', waf, domain='api.lenta.com', path='/')

        # 3) Выбираем пункт самовывоза
        pick_path = f'/v1/stores/pickup/{self.pickup_store}'
        pick_url = Config.API_HOST + pick_path
        r = self.session.put(pick_url, headers=self._make_headers(pick_path, Config.API_HOST))
        r.raise_for_status()
        logger.info("Pickup store %s selected", self.pickup_store)

    def _get_page(self, offset: int, limit: int = 100) -> Dict[str, Any]:
        path = '/v1/catalog/items'
        url = Config.API_HOST + path
        payload = {
            "categoryId": self.category_id,
            "filters": {"multicheckbox": [], "checkbox": [], "range": []},
            "sort": {"type": "popular", "order": "desc"},
            "limit": limit,
            "offset": offset
        }
        r = self.session.post(url, headers=self._make_headers(path, Config.API_HOST), json=payload)
        r.raise_for_status()
        return r.json()

    def fetch_all_items(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        offset = 0
        limit = 100

        while True:
            data = self._get_page(offset, limit)
            batch = data.get('items', [])
            # Оставляем только в наличии
            available = [i for i in batch if i.get('count', 0) > 0]
            items.extend(available)
            logger.info("Fetched %d available items (batch size %d)", len(available), len(batch))

            if len(batch) < limit:
                break
            offset += limit

        logger.info("Total available items: %d", len(items))
        return items


class ExcelExporter:
    @staticmethod
    def export(items: List[Dict[str, Any]], filename: str) -> None:
        rows = []
        for it in items:
            # API возвращает price и priceRegular в копейках, делим на 100
            raw_price   = it.get('prices', {}).get('price', 0) or 0
            raw_regular = it.get('prices', {}).get('priceRegular', 0) or 0

            price   = raw_price   / 100.0
            regular = raw_regular / 100.0

            rows.append({
                'ID':           it.get('id'),
                'Name':         it.get('name'),
                'Price':        f"{price:.2f} ₽",
                'RegularPrice': f"{regular:.2f} ₽",
                'Count':        it.get('count'),
                'Rating':       it.get('rating', {}).get('rate'),
                'Votes':        it.get('rating', {}).get('votes'),
                'Slug':         it.get('slug'),
                'URL':          f"https://lenta.com/{it.get('slug')}"
            })

        df = pd.DataFrame(rows)
        df.to_excel(filename, index=False)
        logger.info("Exported %d records to %s", len(rows), filename)



def main():
    parser = argparse.ArgumentParser(description="Скрипт выгрузки товаров Lenta в Excel")
    parser.add_argument('--pickup', type=int, default=4171, help="ID пункта самовывоза")
    parser.add_argument('--category', type=int, default=21675, help="ID категории")
    parser.add_argument('--output', type=str, default='output.xlsx', help="Имя выходного файла")
    args = parser.parse_args()

    client = LentaApiClient(pickup_store=args.pickup, category_id=args.category)
    try:
        client.init_session()
        items = client.fetch_all_items()
        ExcelExporter.export(items, args.output)
    except requests.HTTPError as e:
        logger.error("HTTP error: %s", e)
    except Exception as e:
        logger.exception("Unexpected error: %s", e)


if __name__ == '__main__':
    main()
