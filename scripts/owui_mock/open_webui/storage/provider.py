"""Mock: хранилище Open WebUI (local)."""


class Storage:
    @staticmethod
    def get_file(file_path):
        return file_path
