'''
Huobi_DM has 3 futures per currency (with USD as base): weekly, bi-weekly(the next week), and quarterly.

You must subscribe to them with: CRY_TC
where
   CRY = BTC, ETC, etc.
   TC is the time code mapping below:
     mapping  = {
         "this_week": "CW", # current week
         "next_week": "NW", # next week
         "quarter": "CQ"    # current quarter
     }

So for example, to get the quarterly BTC future, you subscribe to "BTC_CQ", and it is returned to channel "market.BTC_CQ. ..."
However since the actual contract changes over time, we want to pubish the pair name using the actual expiry date, which
is contained in the exchanges "contract_code".

Here's what you get for BTC querying https://www.hbdm.com/api/v1/contract_contract_info on 2019 Aug 16:
[{"symbol":"BTC","contract_code":"BTC190816","contract_type":"this_week","contract_size":100.000000000000000000,"price_tick":0.010000000000000000,"delivery_date":"20190816","create_date":"20190802","contract_status":1}
,{"symbol":"BTC","contract_code":"BTC190823","contract_type":"next_week","contract_size":100.000000000000000000,"price_tick":0.010000000000000000,"delivery_date":"20190823","create_date":"20190809","contract_status":1}
,{"symbol":"BTC","contract_code":"BTC190927","contract_type":"quarter","contract_size":100.000000000000000000,"price_tick":0.010000000000000000,"delivery_date":"20190927","create_date":"20190614","contract_status":1},
...]
So we return BTC190927 as the pair name for the BTC quaterly future.

'''
import logging
from yapic import json
from decimal import Decimal
import zlib

from sortedcontainers import SortedDict as sd

from cryptofeed.defines import HUOBI_DM, BUY, SELL, TRADES, BID, ASK, L2_BOOK, USER_TRADES
from cryptofeed.feed import Feed
from cryptofeed.standards import pair_std_to_exchange, pair_exchange_to_std, timestamp_normalize, feed_to_exchange
import base64
import hmac
import hashlib
import json
import datetime
import time
import urllib
import asyncio
from collections import namedtuple


LOG = logging.getLogger('feedhandler')

OrderMatch = namedtuple('OrderMatch', ['trade_id', 'trade_amount', 'trade_price', 'trade_type', 'timestamp' ])


class HuobiDM(Feed):
    id = HUOBI_DM

    def __init__(self, pairs=None, channels=None, callbacks=None, config=None, **kwargs):
        if 'address' not in kwargs:
            kwargs['address']='wss://www.hbdm.com/ws'
        super().__init__(pairs=pairs, channels=channels, callbacks=callbacks, config=config, **kwargs)

    def __reset(self):
        self.l2_book = {}

    async def _book(self, msg: dict, timestamp: float):
        """
        {
            'ch':'market.BTC_CW.depth.step0',
            'ts':1565857755564,
            'tick':{
                'mrid':14848858327,
                'id':1565857755,
                'bids':[
                    [  Decimal('9829.99'), 1], ...
                ]
                'asks':[
                    [ 9830, 625], ...
                ]
            },
            'ts':1565857755552,
            'version':1565857755,
            'ch':'market.BTC_CW.depth.step0'
        }
        """
        pair = pair_std_to_exchange(msg['ch'].split('.')[1], self.id)
        data = msg['tick']
        forced = pair not in self.l2_book

        update = {
            BID: sd({
                Decimal(price): Decimal(amount)
                for price, amount in data['bids']
            }),
            ASK: sd({
                Decimal(price): Decimal(amount)
                for price, amount in data['asks']
            })
        }

        if not forced:
            self.previous_book[pair] = self.l2_book[pair]
        self.l2_book[pair] = update

        await self.book_callback(self.l2_book[pair], L2_BOOK, pair, forced, False, timestamp_normalize(self.id, msg['ts']), timestamp)

    async def _trade(self, msg: dict, timestamp: float):
        """
        {
            'ch': 'market.btcusd.trade.detail',
            'ts': 1549773923965,
            'tick': {
                'id': 100065340982,
                'ts': 1549757127140,
                'data': [{'id': '10006534098224147003732', 'amount': Decimal('0.0777'), 'price': Decimal('3669.69'), 'direction': 'buy', 'ts': 1549757127140}]}
        }
        """
        for trade in msg['tick']['data']:
            await self.callback(TRADES,
                                feed=self.id,
                                pair=pair_std_to_exchange(msg['ch'].split('.')[1], self.id),
                                order_id=trade['id'],
                                side=BUY if trade['direction'] == 'buy' else SELL,
                                amount=Decimal(trade['amount']),
                                price=Decimal(trade['price']),
                                timestamp=timestamp_normalize(self.id, trade['ts']),
                                receipt_timestamp=timestamp
                                )

    async def message_handler(self, msg: str, timestamp: float):
        # unzip message
        msg = zlib.decompress(msg, 16 + zlib.MAX_WBITS)
        msg = json.loads(msg, parse_float=Decimal)

        # Huobi sends a ping evert 5 seconds and will disconnect us if we do not respond to it
        if 'ping' in msg:
            await self.websocket.send(json.dumps({'pong': msg['ping']}))
        elif 'status' in msg and msg['status'] == 'ok':
            return
        elif 'ch' in msg:
            if 'trade' in msg['ch']:
                await self._trade(msg, timestamp)
            elif 'depth' in msg['ch']:
                await self._book(msg, timestamp)
            else:
                LOG.warning("%s: Invalid message type %s", self.id, msg)
        elif 'op' in msg:
            if msg['op']=='notify':
                if msg['topic'].startswith('orders'):
                    await self._user_trade(msg, timestamp)
            elif msg['op']=='auth' or msg['op']=='sub':
                if msg[ 'err-code']==0:
                    LOG.info("%s: op success %s", self.id, msg)
                else:
                    LOG.error("%s: op fail %s", self.id, msg)
            elif msg['op']=='ping':
                await self.websocket.send(json.dumps({"op": "pong","ts": int(time.time()*1000)}))
            else:
                LOG.warning("%s: Invalid message type %s", self.id, msg)
        else:
            LOG.warning("%s: Invalid message type %s", self.id, msg)

    async def _user_trade(self, msg, timestamp):
        feed=self.id
        pair=msg['topic'].split('.')[1]

        trades=[]
        for trade in msg["trade"]:
            _trade=OrderMatch(trade_id=trade["trade_id"],
                  trade_amount=trade["trade_volume"],
                  trade_price=trade["trade_price"], trade_type=trade["role"],
                  timestamp=trade['created_at']/1000.0)
            trades.append(_trade)
        await self.callback(USER_TRADES, feed=feed, pair=pair, order_id=msg['order_id'], 
              side=msg['direction'],
              trade_amount=msg["trade_volume"], 
              price=msg["price"], 
              status=msg['status'],
              trades=trades,
              timestamp=msg['created_at']/1000.0, 
              receipt_timestamp = timestamp)

    async def subscribe(self, websocket):
        self.websocket = websocket
        self.__reset()
        client_id = 0
        for chan in self.channels if self.channels else self.config:
            for pair in self.pairs if self.pairs else self.config[chan]:
                pair = pair_exchange_to_std(pair)
                client_id += 1
                if chan==feed_to_exchange(self.id, USER_TRADES):
                    await self.send_auth(websocket)
                    await websocket.send(json.dumps(
                        {
                            "op": "sub",
                            "topic": f'{chan}.{pair}',
                            "cid": str(client_id)
                        }
                    ))
                else:
                    await websocket.send(json.dumps(
                        {
                            "sub": f"market.{pair}.{chan}",
                            "id": str(client_id)
                        }
                    ))
    async def send_auth(self,websocket):
        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        data = {
            "AccessKeyId": self.api_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": timestamp
        }
        sign = self.generate_signature("GET", data, '/'+self.address.split('/')[-1])
        data["op"] = "auth"
        data["type"] = "api"
        data["Signature"] = sign
        await websocket.send(json.dumps(data))
    
    def generate_signature(self, method, params, request_path):
        host_url = urllib.parse.urlparse(self.address).hostname.lower()
        sorted_params = sorted(params.items(), key=lambda d: d[0], reverse=False)
        encode_params = urllib.parse.urlencode(sorted_params)
        payload = [method, host_url, request_path, encode_params]
        payload = "\n".join(payload)
        payload = payload.encode(encoding="UTF8")
        secret_key = self.api_secret.encode(encoding="utf8")
        digest = hmac.new(secret_key, payload, digestmod=hashlib.sha256).digest()
        signature = base64.b64encode(digest)
        signature = signature.decode()
        return signature
