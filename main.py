import asyncio
import json
import os
import re
import socket
from json import JSONDecodeError
from typing import List, Optional

import aiohttp
from datauri import datauri
from aiohttp import ClientSession, InvalidURL, ClientConnectorError, ContentTypeError
import queue


HEADER = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
}

API_KEY = os.getenv("API_KEY")
API_URI = os.getenv("API_URI")
IPFS_GATEWAY = os.getenv("IPFS_GATEWAY")
MAX_REQUESTS = int(os.getenv("MAX_REQUESTS")) if os.getenv("MAX_REQUESTS") else 200

if not API_URI:
    raise RuntimeError(f"No API_URI set!")

if not API_KEY:
    raise RuntimeError(f"No API_KEY set!")

if not IPFS_GATEWAY:
    raise RuntimeError(f"No IPFS_GATEWAY set!")


STATUS_CODE_NO_RESULT = 0
STATUS_CODE_NOT_PARSABLE = 1
STATUS_CODE_INVALID_URI = 2
STATUS_CODE_SERVICE_NOT_FOUND = 3
STATUS_CODE_INVALID_IPFS = 4
STATUS_CODE_UNKNOWN_PROTOCOL = 5
STATUS_CODE_NO_JSON = 1


input_queue = queue.Queue()
output_queue = queue.Queue()

class WorkloadItem:


    def __init__(self, contract_hash: str, token_id: str, token_uri: str):
        self.contract_hash = contract_hash
        self.token_id = token_id
        self.token_uri = token_uri
        self.response_code: Optional[int] = None
        self.metadata: Optional[str] = None

    def set_metadata(self, response_code: int, metadata: Optional[str]):
        self.response_code = response_code
        self.metadata = metadata

    def to_dict(self):
        return {
            "ContractHash": self.contract_hash,
            "TokenId": self.token_id,
            "Code": self.response_code,
            "Metadata": self.metadata,
        }

    @staticmethod
    def from_json(data: dict) -> 'WorkloadItem':
        return WorkloadItem(data["contractHash"], data["tokenId"], data["tokenUri"])


async def get_workload() -> List[WorkloadItem]:
    async with ClientSession() as session:
        async with session.get(f"{API_URI}/api/v1/token/batch", headers={'X-API-KEY': API_KEY, 'accept': 'application/json'}) as response:
            data = await response.json()
            return [WorkloadItem.from_json(entry) for entry in data["tokens"]]


def try_get_data(token_uri: str) -> Optional[str]:
    try:
        data = datauri.parse(token_uri)
        content: bytes = data.data
        return content.decode("UTF-8")
    except:
        return None


def replace_ipfs_gateway(token_uri: str):
    found = re.search("\/([a-zA-Z0-9]{46}|[a-z0-9]{59})(\/|$){1}", token_uri)
    if not found:
        return token_uri

    return f"{IPFS_GATEWAY}{token_uri[found.start():]}"


async def request_metadata(token_uri: str):
    if token_uri.lower().startswith("ipfs://"):
        return STATUS_CODE_INVALID_IPFS, json.dumps({"error": "invalid ipfs link"})
    if token_uri.lower().startswith("ar://"):
        return STATUS_CODE_UNKNOWN_PROTOCOL, json.dumps({"error": "unknown protocol"})
    try:
        async with ClientSession() as session:
            async with session.get(token_uri, headers=HEADER) as response:
                try:
                    return response.status, json.dumps(await response.json())
                except JSONDecodeError:
                    return STATUS_CODE_NOT_PARSABLE, json.dumps({"error": "result is not parsable"})
                except ContentTypeError:
                    return STATUS_CODE_NO_JSON, json.dumps({"error": "result is no json"})
    except ClientConnectorError:
        return STATUS_CODE_SERVICE_NOT_FOUND, json.dumps({"error": "service not found"})
    except InvalidURL:
        return STATUS_CODE_INVALID_URI, json.dumps({"error": "invalid URI"})
    except Exception as e:
        return None, None


async def save_workload(items: List[WorkloadItem]):
    data = [item.to_dict() for item in items]
    async with ClientSession() as session:
        async with session.post(f"{API_URI}/api/v1/token/persist_md", data=json.dumps(data), headers={'X-API-KEY': API_KEY, 'accept': 'application/json', 'Content-Type': 'application/json'}) as response:
            if response.status == 200:
                print(f"Saving successful")
            else:
                print(response.status, await response.json())


async def worker():

    while True:
        if input_queue.qsize() == 0:
            await asyncio.sleep(1)
            continue

        try:
            item: WorkloadItem = input_queue.get_nowait()
            token_uri = item.token_uri

            data = try_get_data(token_uri)
            if data:
                code = 200
            else:

                token_uri = replace_ipfs_gateway(token_uri)
                code, data = await request_metadata(token_uri)
                if not code and not data:
                    code = 0
                    data = json.dumps({"error": "no response"})

            item.set_metadata(code, data)
            output_queue.put_nowait(item)
        except Exception as e:
            raise e


async def save_worker():
    while True:
        try:
            size = min(output_queue.qsize(), 100)
            if size == 0:
                await asyncio.sleep(5)
                continue

            items: List[WorkloadItem] = []
            for _ in range(size):
                item = output_queue.get_nowait()
                if item:
                    items.append(item)

            if items:
                for item in items:
                    uri = item.token_uri
                    if len(uri) > 32:
                        uri = item.token_uri[:29] + "..."
                    metadata = item.metadata
                    if len(metadata) > 160:
                        metadata = metadata[:157] + "..."
                    print(f"[{uri:32s}]: {item.response_code:3d} - {metadata}")

                await save_workload(items)

            if size < 100:
                await asyncio.sleep(5)
        except Exception as e:
            raise e


async def main():
    print(f"Start Metadata Crawler with max. {MAX_REQUESTS} parallel requests...")
    loop = asyncio.get_event_loop()
    for _ in range(200):
        loop.create_task(worker())

    loop.create_task(save_worker())

    while True:
        if input_queue.qsize() > 10000:
            await asyncio.sleep(1)
            continue

        workload = await get_workload()
        if not workload:
            if output_queue.qsize() == 0 and input_queue.qsize() == 0:
                return
            else:
                await asyncio.sleep(1)

        for item in workload:
            input_queue.put(item, block=False)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())