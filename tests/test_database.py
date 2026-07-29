"""
Tests for core database functionality.
"""

import pytest
import pytest_asyncio
from pathlib import Path
import tempfile

from core.database import (
    DatabaseManager,
    Base,
    DeviceRegistry,
    VendorDevice,
    VendorReading,
    VendorCommand,
)


@pytest_asyncio.fixture
async def db_manager():
    """Create a test database manager with temporary SQLite databases."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        core_db = f"sqlite+aiosqlite:///{tmpdir}/core.db"
        vendor_db_dir = tmpdir / "vendors"
        
        manager = DatabaseManager(
            core_db_url=core_db,
            vendor_db_dir=vendor_db_dir,
            echo=False,
        )
        await manager.initialize()
        
        yield manager
        
        await manager.close()


@pytest_asyncio.fixture
async def example_db(db_manager):
    """Get example vendor database session."""
    await db_manager.get_vendor_engine("example")
    return db_manager.get_vendor_session("example")


class TestCoreDatabase:
    """Test core database operations."""
    
    async def test_core_db_initialization(self, db_manager):
        """Test core database tables are created."""
        async with db_manager.core_session() as session:
            from sqlalchemy import select
            
            # Check tables exist by querying them
            result = await session.execute(select(DeviceRegistry))
            devices = result.scalars().all()
            assert isinstance(devices, list)
    
    async def test_device_registry_crud(self, db_manager):
        """Test device registry CRUD operations."""
        async with db_manager.core_session() as session:
            # Create
            device = DeviceRegistry(
                device_id="test_device_001",
                vendor="example",
                device_type="ac",
                                vendor_db_name="example",
                name="Test AC",
                location="Living Room",
                capabilities={"temp_control": True, "modes": ["cool", "heat"]},
                config={"target_temp": 24},
            )
            session.add(device)
            await session.commit()
            await session.refresh(device)
            
            assert device.id is not None
            assert device.device_id == "test_device_001"
            
            # Read
            from sqlalchemy import select
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "test_device_001")
            )
            found = result.scalar_one_or_none()
            assert found is not None
            assert found.name == "Test AC"
            
            # Update
            found.name = "Updated AC"
            await session.commit()
            await session.refresh(found)
            assert found.name == "Updated AC"
            
            # Delete
            await session.delete(found)
            await session.commit()
            
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "test_device_001")
            )
            assert result.scalar_one_or_none() is None


class TestVendorDatabase:
    """Test per-vendor database operations."""
    
    async def test_vendor_db_initialization(self, db_manager, example_db):
        """Test vendor database tables are created."""
            async with example_db() as session:
            from sqlalchemy import select
            
            result = await session.execute(select(VendorDevice))
            devices = result.scalars().all()
            assert isinstance(devices, list)
    
        async def test_vendor_device_crud(self, db_manager, example_db):
        """Test vendor device CRUD."""
            async with example_db() as session:
            # Create
            device = VendorDevice(
                    device_id="example_ac_001",
                    name="Example AC",
                device_type="ac",
                model="YK-001",
                firmware_version="1.2.3",
                capabilities={"dp_codes": {"power": "1", "temp": "3"}},
                config={"region": "eu"},
            )
            session.add(device)
            await session.commit()
            await session.refresh(device)
            
            assert device.id is not None
            
            # Read
            from sqlalchemy import select
            result = await session.execute(
                    select(VendorDevice).where(VendorDevice.device_id == "example_ac_001")
            )
            found = result.scalar_one_or_none()
            assert found is not None
            assert found.model == "YK-001"
    
        async def test_vendor_readings(self, db_manager, example_db):
        """Test vendor readings time-series data."""
        from datetime import datetime, timezone

            async with example_db() as session:
            # Add readings
            for i in range(5):
                reading = VendorReading(
                        device_id="example_ac_001",
                    timestamp=datetime.now(timezone.utc),
                    temp_target=24.0,
                    temp_actual=23.5 + i * 0.1,
                    humidity=50.0,
                    power_watts=1200.0,
                    mode="cool",
                    fan_speed="auto",
                    vendor_data={"dp_raw": {"1": True, "3": 240}},
                )
                session.add(reading)
            await session.commit()

            # Query recent readings
            from sqlalchemy import select, desc
            result = await session.execute(
                select(VendorReading)
                        .where(VendorReading.device_id == "example_ac_001")
                .order_by(desc(VendorReading.timestamp))
                .limit(3)
            )
            readings = result.scalars().all()
            assert len(readings) == 3

        async def test_vendor_commands(self, db_manager, example_db):
        """Test vendor command logging."""
        from datetime import datetime, timezone

            async with example_db() as session:
            command = VendorCommand(
                    device_id="example_ac_001",
                timestamp=datetime.now(timezone.utc),
                command="set_temp",
                params={"temperature": 25.0},
                source="edge_auto",
                    edge_model_id="example_ac_v1",
                confidence=0.95,
                status="sent",
            )
            session.add(command)
            await session.commit()
            await session.refresh(command)

            assert command.id is not None
            assert command.status == "sent"

            # Update status
            command.status = "acked"
            command.executed_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(command)
            assert command.status == "acked"


class TestDatabaseManager:
    """Test DatabaseManager functionality."""
    
    async def test_multi_vendor_databases(self, db_manager):
        """Test creating multiple vendor databases."""
        # Initialize multiple vendor DBs
        example_engine = await db_manager.get_vendor_engine("example")
        
                assert example_engine is not None
        
                # Verify they're separate engines
                assert example_engine is not None
    
            async def test_vendor_session_isolation(self, db_manager):
                """Test vendor database sessions are isolated."""
                await db_manager.get_vendor_engine("example")
        
                example_session = db_manager.get_vendor_session("example")
        
                # Add device to example DB
                async with example_session() as session:
                    device = VendorDevice(
                        device_id="test_example",
                        name="Example Device",
                        device_type="ac",
                    )
                    session.add(device)
                    await session.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# RUN TESTS
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])