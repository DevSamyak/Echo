import base64

from pyDes import ECB, PAD_PKCS5, des


class CryptoService:
    """
    Service responsible for decrypting Saavn media URLs.
    """

    @staticmethod
    def decrypt_url(url: str) -> str:
        """
        Decrypt the encrypted media URL.
        Args:
            url (str): Encrypted media URL
        Returns:
            str: Decrypted media URL
        """
        try:
            des_cipher = des(
                b"38346591",
                ECB,
                b"\0\0\0\0\0\0\0\0",
                pad=None,
                padmode=PAD_PKCS5,
            )
            enc_url = base64.b64decode(url.strip())
            dec_url = des_cipher.decrypt(enc_url, padmode=PAD_PKCS5).decode(
                "utf-8"
            )
            return dec_url.replace("_96.mp4", "_320.mp4")
        except Exception as e:
            raise ValueError(f"URL decryption failed: {str(e)}") from e
