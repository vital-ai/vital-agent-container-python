import os
import pytest
import jwt
import json
import httpx
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock
from vital_agent_container.utils.jwt_utils import JWTUtils, JWTValidationError, JWTExpiredError, JWTInvalidClaimsError
from vital_agent_container.agent_container_app import AgentContainerApp
from vital_agent_container.handler.impl.aimp_echo_message_handler import AIMPEchoMessageHandler


class TestJWTUtils:
    """Test JWT utility functions"""
    
    def setup_method(self):
        """Setup test fixtures"""
        self.secret_key = "test-secret-key-for-jwt-testing"
        self.jwt_config = {
            'enabled': True,
            'secret_key': self.secret_key,
            'algorithm': 'HS256',
            'enforcement_mode': 'header',
            'required_claims': ['sub', 'exp', 'iat'],
            'custom_claims': ['user_id', 'permissions']
        }
        
        # Create a valid test JWT
        self.valid_payload = {
            'sub': 'test-user-123',
            'exp': datetime.now(timezone.utc) + timedelta(hours=1),
            'iat': datetime.now(timezone.utc),
            'user_id': 'user123',
            'permissions': ['read', 'write']
        }
        self.valid_token = jwt.encode(self.valid_payload, self.secret_key, algorithm='HS256')
        
        # Create an expired JWT
        expired_payload = {
            'sub': 'test-user-123',
            'exp': datetime.now(timezone.utc) - timedelta(hours=1),
            'iat': datetime.now(timezone.utc) - timedelta(hours=2),
        }
        self.expired_token = jwt.encode(expired_payload, self.secret_key, algorithm='HS256')
    
    async def test_validate_valid_jwt_token(self):
        """Test validation of a valid JWT token"""
        result = await JWTUtils.validate_jwt_token(self.valid_token, self.jwt_config)

        assert result['sub'] == 'test-user-123'
        assert result['user_id'] == 'user123'
        assert result['permissions'] == ['read', 'write']

    async def test_validate_jwt_token_with_bearer_prefix(self):
        """Test validation of JWT token with Bearer prefix"""
        bearer_token = f"Bearer {self.valid_token}"
        result = await JWTUtils.validate_jwt_token(bearer_token, self.jwt_config)

        assert result['sub'] == 'test-user-123'

    async def test_validate_expired_jwt_token(self):
        """Test validation of expired JWT token"""
        with pytest.raises(JWTExpiredError):
            await JWTUtils.validate_jwt_token(self.expired_token, self.jwt_config)

    async def test_validate_invalid_jwt_token(self):
        """Test validation of invalid JWT token"""
        with pytest.raises(JWTValidationError):
            await JWTUtils.validate_jwt_token("invalid.jwt.token", self.jwt_config)

    async def test_validate_jwt_token_missing_claims(self):
        """Test validation of JWT token with missing required claims"""
        payload = {
            'sub': 'test-user-123',
            'exp': datetime.now(timezone.utc) + timedelta(hours=1),
            # Missing 'iat' claim
        }
        token = jwt.encode(payload, self.secret_key, algorithm='HS256')

        with pytest.raises(JWTInvalidClaimsError):
            await JWTUtils.validate_jwt_token(token, self.jwt_config)
    
    def test_extract_user_permissions(self):
        """Test extraction of user permissions from JWT payload"""
        permissions = JWTUtils.extract_user_permissions(self.valid_payload)
        assert permissions == ['read', 'write']
        
        # Test with empty permissions
        empty_payload = {'sub': 'test'}
        permissions = JWTUtils.extract_user_permissions(empty_payload)
        assert permissions == []
    
    def test_extract_user_id(self):
        """Test extraction of user ID from JWT payload"""
        user_id = JWTUtils.extract_user_id(self.valid_payload)
        assert user_id == 'test-user-123'  # Should use 'sub' claim
        
        # Test with user_id claim
        payload_with_user_id = {'user_id': 'user456'}
        user_id = JWTUtils.extract_user_id(payload_with_user_id)
        assert user_id == 'user456'
    
    def test_has_permission(self):
        """Test permission checking"""
        assert JWTUtils.has_permission(self.valid_payload, 'read') == True
        assert JWTUtils.has_permission(self.valid_payload, 'write') == True
        assert JWTUtils.has_permission(self.valid_payload, 'admin') == False
    
    def test_validate_jwt_config(self):
        """Test JWT configuration validation"""
        # Valid config
        assert JWTUtils.validate_jwt_config(self.jwt_config) == True
        
        # Disabled JWT
        disabled_config = {'enabled': False}
        assert JWTUtils.validate_jwt_config(disabled_config) == True
        
        # Invalid enforcement mode
        invalid_config = {
            'enabled': True,
            'secret_key': 'test',
            'algorithm': 'HS256',
            'enforcement_mode': 'invalid'
        }
        with pytest.raises(ValueError):
            JWTUtils.validate_jwt_config(invalid_config)


class TestAgentContainerAppJWT:
    """Test JWT enforcement in AgentContainerApp"""
    
    def setup_method(self):
        """Setup test fixtures"""
        self.handler = AIMPEchoMessageHandler()
        
        # Test with JWT enabled
        self.jwt_config = {
            'enabled': True,
            'secret_key': 'test-secret-key',
            'algorithm': 'HS256',
            'enforcement_mode': 'header',
            'required_claims': ['sub', 'exp', 'iat'],
            'custom_claims': []
        }
        
        self.app = AgentContainerApp(
            handler=self.handler,
            app_config={},
            jwt_config=self.jwt_config
        )
    
    def test_load_jwt_config_from_env(self):
        """Test loading JWT config from environment variables"""
        with patch.dict(os.environ, {
            'JWT_ENABLED': 'true',
            'JWT_SECRET_KEY': 'env-secret',
            'JWT_ALGORITHM': 'HS256',
            'JWT_ENFORCEMENT_MODE': 'header',
            'JWT_REQUIRED_CLAIMS': 'sub,exp,iat,custom',
            'JWT_CUSTOM_CLAIMS': 'user_id,permissions'
        }):
            app = AgentContainerApp(handler=self.handler, app_config={})
            
            assert app.jwt_config['enabled'] == True
            assert app.jwt_config['secret_key'] == 'env-secret'
            assert app.jwt_config['algorithm'] == 'HS256'
            assert app.jwt_config['enforcement_mode'] == 'header'
            assert app.jwt_config['required_claims'] == ['sub', 'exp', 'iat', 'custom']
            assert app.jwt_config['custom_claims'] == ['user_id', 'permissions']
    
    @pytest.mark.asyncio
    async def test_validate_jwt_token(self):
        """Test JWT token validation in AgentContainerApp"""
        # Create a valid token
        payload = {
            'sub': 'test-user',
            'exp': datetime.now(timezone.utc) + timedelta(hours=1),
            'iat': datetime.now(timezone.utc)
        }
        token = jwt.encode(payload, 'test-secret-key', algorithm='HS256')
        
        result = await self.app.validate_jwt_token(token)
        assert result['sub'] == 'test-user'
        
        # Test with None token
        result = await self.app.validate_jwt_token(None)
        assert result is None
    
    @pytest.mark.asyncio
    async def test_extract_jwt_from_message_payload(self):
        """Test JWT extraction from message payload"""
        # Create test message with JWT
        jwt_data = {
            'jwt_token': jwt.encode({'sub': 'transport', 'exp': datetime.now(timezone.utc) + timedelta(hours=1), 'iat': datetime.now(timezone.utc)}, 'test-secret-key', algorithm='HS256'),
            'jwt_auth_token': jwt.encode({'sub': 'user', 'exp': datetime.now(timezone.utc) + timedelta(hours=1), 'iat': datetime.now(timezone.utc), 'permissions': ['read']}, 'test-secret-key', algorithm='HS256')
        }
        
        message = [
            {
                'http://vital.ai/ontology/vital-aimp#hasJwtJSON': json.dumps(jwt_data)
            }
        ]
        
        jwt_payloads, jwt_tokens = await self.app.extract_jwt_from_message_payload(json.dumps(message))

        assert jwt_payloads is not None
        assert 'jwt_token' in jwt_payloads
        assert 'jwt_auth_token' in jwt_payloads
        assert jwt_payloads['jwt_token']['sub'] == 'transport'
        assert jwt_payloads['jwt_auth_token']['sub'] == 'user'
        assert jwt_payloads['jwt_auth_token']['permissions'] == ['read']

        # Raw tokens are returned alongside the decoded payloads
        assert jwt_tokens['jwt_token'] == jwt_data['jwt_token']
        assert jwt_tokens['jwt_auth_token'] == jwt_data['jwt_auth_token']
    
    @pytest.mark.asyncio
    async def test_extract_jwt_from_invalid_message(self):
        """Test JWT extraction from invalid message"""
        # Test with invalid JSON
        jwt_payloads, jwt_tokens = await self.app.extract_jwt_from_message_payload("invalid json")
        assert jwt_payloads is None
        assert jwt_tokens is None

        # Test with message without JWT
        message = [{'type': 'test'}]
        jwt_payloads, jwt_tokens = await self.app.extract_jwt_from_message_payload(json.dumps(message))
        assert jwt_payloads is None
        assert jwt_tokens is None


@pytest.mark.asyncio
async def test_websocket_header_authentication():
    """Test WebSocket authentication via headers"""
    # This would require more complex WebSocket testing setup
    # For now, we'll test the core JWT validation logic
    pass


@pytest.mark.asyncio
async def test_websocket_payload_authentication():
    """Test WebSocket authentication via message payload"""
    # This would require more complex WebSocket testing setup
    # For now, we'll test the core JWT extraction logic
    pass


if __name__ == "__main__":
    pytest.main([__file__])
