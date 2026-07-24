import os
import jwt
import logging
import httpx
import json
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import base64


logger = logging.getLogger("VitalAgentContainerLogger")


class JWTValidationError(Exception):
    """Base exception for JWT validation errors"""
    pass


class JWTExpiredError(JWTValidationError):
    """JWT token has expired"""
    pass


class JWTInvalidClaimsError(JWTValidationError):
    """JWT token has invalid or missing required claims"""
    pass


class JWTUtils:
    """Utility class for JWT token validation and processing"""
    
    _jwks_cache = {}  # Cache for JWKS keys
    _cache_ttl = 3600  # Cache TTL in seconds (1 hour)
    
    @staticmethod
    async def fetch_jwks_key(jwks_url: str, kid: str) -> str:
        """
        Fetch a specific key from JWKS endpoint
        
        Args:
            jwks_url: URL to the JWKS endpoint
            kid: Key ID to fetch
            
        Returns:
            PEM-formatted public key string
            
        Raises:
            JWTValidationError: If key cannot be fetched or parsed
        """
        cache_key = f"{jwks_url}#{kid}"
        
        # Check cache first
        if cache_key in JWTUtils._jwks_cache:
            cached_entry = JWTUtils._jwks_cache[cache_key]
            if datetime.now().timestamp() - cached_entry['timestamp'] < JWTUtils._cache_ttl:
                logger.debug(f"Using cached JWKS key for kid: {kid}")
                return cached_entry['key']
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(jwks_url, timeout=10.0)
                response.raise_for_status()
                jwks_data = response.json()
            
            # Find the key with matching kid
            for key_data in jwks_data.get('keys', []):
                if key_data.get('kid') == kid:
                    # Convert JWK to PEM format
                    if key_data.get('kty') == 'RSA':
                        pem_key = JWTUtils._jwk_to_pem(key_data)
                        
                        # Cache the key
                        JWTUtils._jwks_cache[cache_key] = {
                            'key': pem_key,
                            'timestamp': datetime.now().timestamp()
                        }
                        
                        logger.info(f"Successfully fetched JWKS key for kid: {kid}")
                        return pem_key
            
            raise JWTValidationError(f"Key with kid '{kid}' not found in JWKS")
            
        except httpx.RequestError as e:
            logger.error(f"Failed to fetch JWKS from {jwks_url}: {str(e)}")
            raise JWTValidationError(f"Failed to fetch JWKS: {str(e)}")
        except Exception as e:
            logger.error(f"Error processing JWKS response: {str(e)}")
            raise JWTValidationError(f"JWKS processing error: {str(e)}")
    
    @staticmethod
    def _jwk_to_pem(jwk_data: Dict[str, Any]) -> str:
        """
        Convert JWK (JSON Web Key) to PEM format
        
        Args:
            jwk_data: JWK data dictionary
            
        Returns:
            PEM-formatted public key string
        """
        try:
            # Extract RSA components
            n = base64.urlsafe_b64decode(jwk_data['n'] + '==')  # Add padding
            e = base64.urlsafe_b64decode(jwk_data['e'] + '==')
            
            # Convert to integers
            n_int = int.from_bytes(n, 'big')
            e_int = int.from_bytes(e, 'big')
            
            # Create RSA public key
            public_key = rsa.RSAPublicNumbers(e_int, n_int).public_key()
            
            # Serialize to PEM format
            pem_bytes = public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )
            
            return pem_bytes.decode('utf-8')
            
        except Exception as e:
            raise JWTValidationError(f"Failed to convert JWK to PEM: {str(e)}")
    
    @staticmethod
    async def validate_jwt_token(token: str, jwt_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate and decode a JWT token
        
        Args:
            token: JWT token string
            jwt_config: JWT configuration dictionary
            
        Returns:
            Dict containing decoded JWT payload
            
        Raises:
            JWTValidationError: If token validation fails
            JWTExpiredError: If token has expired
            JWTInvalidClaimsError: If required claims are missing
        """
        try:
            # Remove 'Bearer ' prefix if present
            if token.startswith('Bearer '):
                token = token[7:]
            
            algorithm = jwt_config.get('algorithm', 'RS256')
            
            # Get the appropriate key for verification
            if algorithm.startswith('RS'):
                # RSA algorithms use public key
                jwks_url = jwt_config.get('jwks_url')
                public_key_path = jwt_config.get('public_key_path')
                
                if jwks_url:
                    # Use JWKS URL to fetch key dynamically
                    # First decode token header to get kid (key ID)
                    try:
                        header = jwt.get_unverified_header(token)
                        kid = header.get('kid')
                        if not kid:
                            raise JWTValidationError("Token header missing 'kid' claim for JWKS lookup")
                        
                        key = await JWTUtils.fetch_jwks_key(jwks_url, kid)
                    except Exception as e:
                        logger.error(f"JWKS key fetch failed: {str(e)}")
                        raise JWTValidationError(f"Failed to fetch JWKS key: {str(e)}")
                        
                elif public_key_path:
                    # Use local public key file
                    if not os.path.exists(public_key_path):
                        raise JWTValidationError(f"Public key file not found: {public_key_path}")
                    
                    with open(public_key_path, 'r') as key_file:
                        key = key_file.read()
                else:
                    raise JWTValidationError("Either 'jwks_url' or 'public_key_path' must be configured for RSA algorithms")
            else:
                # HMAC algorithms use secret key
                key = jwt_config.get('secret_key')
                if not key:
                    raise JWTValidationError("Secret key not configured for HMAC algorithm")
            
            # Configure JWT decode options
            decode_options = {"verify_exp": True, "verify_iat": True}
            
            # Handle audience validation
            audience = jwt_config.get('audience')
            if audience:
                # If audience is configured, verify it
                payload = jwt.decode(
                    token,
                    key,
                    algorithms=[algorithm],
                    audience=audience,
                    options=decode_options
                )
            else:
                # If no audience configured, skip audience verification
                decode_options["verify_aud"] = False
                payload = jwt.decode(
                    token,
                    key,
                    algorithms=[algorithm],
                    options=decode_options
                )
            
            # Validate required claims
            required_claims = jwt_config.get('required_claims', ['sub', 'exp', 'iat'])
            missing_claims = [claim for claim in required_claims if claim not in payload]
            if missing_claims:
                raise JWTInvalidClaimsError(f"Missing required claims: {missing_claims}")
            
            # Log successful validation (without sensitive data)
            logger.info(f"JWT token validated successfully for subject: {payload.get('sub', 'unknown')}")
            
            return payload
            
        except jwt.ExpiredSignatureError:
            logger.warning("JWT token has expired")
            raise JWTExpiredError("JWT token has expired")
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {str(e)}")
            raise JWTValidationError(f"Invalid JWT token: {str(e)}")
        except JWTValidationError:
            # Preserve our own validation exceptions (e.g. JWTInvalidClaimsError)
            # instead of flattening them into a generic JWTValidationError below.
            raise
        except Exception as e:
            logger.error(f"JWT validation error: {str(e)}")
            raise JWTValidationError(f"JWT validation failed: {str(e)}")
    
    @staticmethod
    def extract_user_permissions(jwt_payload: Dict[str, Any]) -> List[str]:
        """
        Extract user permissions from JWT payload
        
        Args:
            jwt_payload: Decoded JWT payload
            
        Returns:
            List of permission strings
        """
        # Try common permission claim names
        permissions = jwt_payload.get('permissions', [])
        if not permissions:
            permissions = jwt_payload.get('perms', [])
        if not permissions:
            permissions = jwt_payload.get('roles', [])
        
        return permissions if isinstance(permissions, list) else []
    
    @staticmethod
    def extract_user_id(jwt_payload: Dict[str, Any]) -> Optional[str]:
        """
        Extract user ID from JWT payload
        
        Args:
            jwt_payload: Decoded JWT payload
            
        Returns:
            User ID string or None
        """
        # Try common user ID claim names
        user_id = jwt_payload.get('sub')  # Standard 'subject' claim
        if not user_id:
            user_id = jwt_payload.get('user_id')
        if not user_id:
            user_id = jwt_payload.get('uid')
        
        return user_id
    
    @staticmethod
    def has_permission(jwt_payload: Dict[str, Any], required_permission: str) -> bool:
        """
        Check if JWT payload contains a specific permission
        
        Args:
            jwt_payload: Decoded JWT payload
            required_permission: Permission string to check for
            
        Returns:
            True if permission is present, False otherwise
        """
        permissions = JWTUtils.extract_user_permissions(jwt_payload)
        return required_permission in permissions
    
    @staticmethod
    def validate_jwt_config(jwt_config: Dict[str, Any]) -> bool:
        """
        Validate JWT configuration
        
        Args:
            jwt_config: JWT configuration dictionary
            
        Returns:
            True if configuration is valid
            
        Raises:
            ValueError: If configuration is invalid
        """
        if not jwt_config.get('enabled', False):
            return True  # Skip validation if JWT is disabled
        
        algorithm = jwt_config.get('algorithm', 'RS256')
        
        if algorithm.startswith('RS'):
            # RSA algorithms require either JWKS URL or public key file
            jwks_url = jwt_config.get('jwks_url')
            public_key_path = jwt_config.get('public_key_path')
            
            if not jwks_url and not public_key_path:
                raise ValueError("Either 'jwks_url' or 'public_key_path' is required for RSA algorithms")
            
            if public_key_path and not os.path.exists(public_key_path):
                raise ValueError(f"Public key file not found: {public_key_path}")
        else:
            # HMAC algorithms require secret key
            secret_key = jwt_config.get('secret_key')
            if not secret_key:
                raise ValueError("secret_key is required for HMAC algorithms")
        
        enforcement_mode = jwt_config.get('enforcement_mode', 'none')
        if enforcement_mode not in ['header', 'payload', 'none']:
            raise ValueError(f"Invalid enforcement_mode: {enforcement_mode}")
        
        logger.info(f"JWT configuration validated: algorithm={algorithm}, mode={enforcement_mode}")
        return True
