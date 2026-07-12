import json
import os
import threading
import time
from loguru import logger as log

import globals as gl
from src.backend.atomic_json import atomic_write_json

class StoreCache:
    # Entries carry two clocks: "date" is LAST USE (refreshed on every open,
    # drives remove_old_cache_files eviction of unused entries) and "fetched"
    # is CONTENT AGE (set only when the file is written, drives the
    # stale-fallback bound in StoreBackend.get_remote_file). Bounding
    # staleness on "date" would be circular: serving the stale copy would
    # keep renewing it.
    DAYS_TO_KEEP = 3

    def __init__(self):
        self.CACHE_PATH = os.path.join(gl.DATA_PATH, "Store" , "cache")

        self.files_json = os.path.join(self.CACHE_PATH, "files.json")
        self.files_dir = os.path.join(self.CACHE_PATH, "files")

        self.write_lock = threading.Lock()

        self.files = self.get_files()
        self.remove_old_cache_files()

        self.create_cache_dirs()
        self.create_cache_files()

    def get_files(self) -> dict:
        if not os.path.exists(self.files_json):
            return {}
        try:
            with open(self.files_json, "r") as f:
                return json.load(f)
        except json.decoder.JSONDecodeError as e:
            log.error(e)
            return {}
        
    def set_files(self, files: dict):
        with self.write_lock:
            atomic_write_json(self.files_json, files.copy())

    def remove_old_cache_files(self):
        for string in self.files.copy():
            path = self.files[string].get("path")
            if not os.path.exists(path):
                continue
            date = self.files[string].get("date")
            if date is None:
                os.remove(path)
                self.files.pop(string)

            if time.time() - date > self.DAYS_TO_KEEP * 24 * 60 * 60:
                os.remove(path)
                self.files.pop(string)

        self.set_files(self.files)

    def create_cache_dirs(self):
        os.makedirs(self.CACHE_PATH, exist_ok=True)

    def create_cache_files(self):
        files = [self.files_json]

        for file in files:
            if not os.path.exists(file):
                atomic_write_json(file, {})

    def get_user_name(self, repo_url:str) -> str:
        splitted =  repo_url.split("/")
        domain = "github.com"
        if domain not in splitted:
            domain = "raw.githubusercontent.com"

        return splitted[splitted.index(domain)+1]
    
    def get_repo_name(self, repo_url:str) -> str:
        github_split = repo_url.split("github")
        if len(github_split) < 2:
            return
        split = github_split[1].split("/")
        if len(split) < 3:
            return
        return split[2]

    def generate_cache_string(self, url: str, path: str, branch: str = "main", data_type: str = "text") -> str:
        user = self.get_user_name(url)
        repo = self.get_repo_name(url)
        return f"{user}::{repo}::{branch}::{data_type}::{path}"
    
    def get_cache_path(self, url: str, path: str, branch: str = "main", data_type: str = "text") -> str:
        # return os.path.join(self.files_dir, self.generate_cache_string(url, path, branch, data_type))

        cache_string = self.generate_cache_string(url, path, branch, data_type)
        if cache_string in self.files:
            return self.files[cache_string].get("path")
        
        else:
            path = os.path.join(self.files_dir, cache_string)
            self.files[cache_string] = {
                "path": path,
                "date": time.time()
            }
            self.set_files(self.files)
            return path
    
    def is_cached(self, url: str, path: str, branch: str = "main", data_type: str = "text") -> bool:
        cache_string = self.generate_cache_string(url, path, branch, data_type)
        if cache_string not in self.files:
            return False
        
        if self.files[cache_string].get("path") is None:
            return False
        
        return os.path.exists(self.files[cache_string].get("path"))

    def open_cache_file(self, url: str, path: str, branch: str = "main", data_type: str = "text", mode: str = "r") -> str:
        cache_path = self.get_cache_path(url, path, branch, data_type)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        entry = self.files.get(self.generate_cache_string(url, path, branch, data_type), {})
        entry["path"] = cache_path
        entry["date"] = time.time()  # last use (eviction clock)
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            entry["fetched"] = time.time()  # content age (staleness clock)
        self.files[self.generate_cache_string(url, path, branch, data_type)] = entry
        self.set_files(self.files)

        return open(cache_path, mode)

    def get_fetched_date(self, url: str, path: str, branch: str = "main", data_type: str = "text") -> float:
        """When the cached content was last WRITTEN (falls back to the
        last-use date for entries predating the "fetched" field); None if
        unknown."""
        entry = self.files.get(self.generate_cache_string(url, path, branch, data_type), {})
        return entry.get("fetched", entry.get("date"))