#import ujson as json
import json

class File:
    @staticmethod
    def readFile(path):
        try:
            with open(path, mode="r") as f:
                data = json.loads(f.read())
            return data
        except IndexError:
            raise IndexError
        except IOError:
            raise IOError
        except OSError:
            raise OSError

    @staticmethod
    def writeFile(data, path):
        # Compact separators keep json on its C-accelerated encoder. Passing
        # indent= (the previous behavior) forces the pure-Python encoder, which
        # is ~6x slower and ~2.5x larger on the multi-MB result/view files
        # written on every run. These files are machine-read by the frontend
        # (JSON.parse), so the dropped whitespace has no consumer.
        try:
            with open(path, mode="w") as f:
                f.write(json.dumps(data, ensure_ascii=True, separators=(",", ":")))
        except (IOError, IndexError):
            raise IndexError
        except OSError:
            raise OSError

    @staticmethod
    def writeFileUJson(data, path):
        try:
            with open(path, mode="w") as f:
                f.write(json.dumps(data))
        except (IOError, IndexError):
            raise IndexError
        except OSError:
            raise OSError

    @staticmethod
    def readParamFile(path):
        try:
            with open(path, mode="r") as f:
                data = json.loads(f.read())
            return data
        except IndexError:
            raise IndexError
        except IOError:
            raise IOError
        except OSError:
            raise OSError