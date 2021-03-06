package com.neon.flume;

import static org.junit.Assert.fail;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.Arrays;

import org.apache.avro.Schema;
import org.apache.avro.file.DataFileStream;
import org.apache.avro.generic.GenericData;
import org.apache.avro.generic.GenericDatumReader;
import org.apache.avro.generic.GenericRecord;
import org.apache.avro.generic.GenericRecordBuilder;
import org.apache.avro.io.BinaryEncoder;
import org.apache.avro.io.DatumReader;
import org.apache.avro.io.EncoderFactory;
import org.apache.avro.reflect.ReflectDatumWriter;
import org.apache.avro.util.Utf8;
import org.apache.commons.io.IOUtils;
import org.apache.flume.Context;
import org.apache.flume.Event;
import org.apache.flume.event.EventBuilder;
import org.apache.flume.serialization.EventSerializer;
import org.apache.log4j.Level;
import org.apache.log4j.Logger;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Assert;
import org.junit.Before;
import org.junit.Test;

import com.neon.Tracker.GeoData;
import com.neon.Tracker.ImagesVisible;
import com.neon.Tracker.TrackerEvent;
import com.neon.flume.NeonAvroEventSerializer.URLOpener;

public class TestNeonAvroEventSerializer {

  private Schema[] schemaArray = new Schema[10];
  private String testRecord;

  private static TestAppender testAppender;
  private static Logger logger;

  @Before
  public void setUp() throws Exception {
    // Add the logger
    testAppender = new TestAppender();
    logger = Logger.getLogger(NeonAvroEventSerializer.class.getName());
    logger.addAppender(testAppender);

    FileInputStream testFile = new FileInputStream(new File("src/test/java/com/neon/flume/TestRecord.avsc"));
    testRecord = IOUtils.toString(testFile, "UTF-8");
  }

  @After
  public void tearDown() {
    // Remove the logger
    logger.removeAppender(testAppender);
  }

  @Test
  public void testOneSchema() throws IOException {
    // Test to ensure that the serializer works in the most basic use case
    ByteArrayOutputStream outStream = new ByteArrayOutputStream();

    Schema schema = null;
    GenericRecord record = null;

    schema = TrackerEvent.getClassSchema();

    ImagesVisible visEvent = new ImagesVisible(true,
        Arrays.asList((CharSequence) "acct1_vid1_thumb1", "acct1_vid2_thumb2"));

    record = buildDefaultGenericEvent(schema).set("eventType", "IMAGE_VISIBLE").set("eventData", visEvent).build();

    File schemaFile = new File("src/test/java/com/neon/flume/TrackerEvent.avsc");

    EventSerializer serializer = createEventSerializer(outStream);
    serializer.afterCreate();

    for (int i = 0; i < 10; i++) {
      Event event = EventBuilder.withBody(serializeAvro(record, schema));
      event.getHeaders().put(NeonAvroEventSerializer.AVRO_SCHEMA_URL_HEADER,
          schemaFile.toURI().toURL().toExternalForm());

      serializer.write(event);
      schemaArray[i] = schema;
    }

    shutDownAll(serializer, outStream);
    validateAvroEvents(outStream);
  }

  @Test
  public void testSchemaEvolution() throws IOException {
    // Test to ensure that the serializer works with multiple schemas
    // The schemas adhere to the rules of schema evolution
    ByteArrayOutputStream outStream = new ByteArrayOutputStream();

    EventSerializer serializer = createEventSerializer(outStream);
    serializer.afterCreate();

    ImagesVisible visEvent = new ImagesVisible(true,
        Arrays.asList((CharSequence) "acct1_vid1_thumb1", "acct1_vid2_thumb2"));

    Schema schema = null;
    GenericRecord record = null;
    JSONObject schemaJson = null;

    File schemaFile = null;
    for (int i = 0; i < 10; i++) {
      if (i < 3 || i > 8) {
        schemaJson = new JSONObject(TrackerEvent.getClassSchema().toString());
        schemaJson.getJSONArray("fields").put(
            new JSONObject("{\"name\": \"dummyField\", \"type\" : [ \"null\" , \"int\" ], \"default\" : \"null\"}"));

        schema = new Schema.Parser().parse(schemaJson.toString());

        record = buildDefaultGenericEvent(schema).set("dummyField", 78).set("eventType", "IMAGE_VISIBLE")
            .set("eventData", visEvent).build();

        schemaFile = new File("src/test/java/com/neon/flume/TrackerEventEvolution.avsc");
      } else {
        schemaJson = new JSONObject(TrackerEvent.getClassSchema().toString());
        schema = new Schema.Parser().parse(schemaJson.toString());

        record = buildDefaultGenericEvent(schema).set("eventType", "IMAGE_VISIBLE").set("eventData", visEvent).build();
        schemaFile = new File("src/test/java/com/neon/flume/TrackerEvent.avsc");
      }

      Event event = EventBuilder.withBody(serializeAvro(record, schema));
      event.getHeaders().put(NeonAvroEventSerializer.AVRO_SCHEMA_URL_HEADER,
          schemaFile.toURI().toURL().toExternalForm());

      serializer.write(event);
      schemaArray[i] = schema;
      serializer.flush();
      outStream.flush();
    }

    shutDownAll(serializer, outStream);
    validateAvroEvents(outStream);
  }

  @Test
  public void testSchemaNoEvolution() throws IOException {
    // Test to ensure that the serializer works with multiple schemas
    // The schemas do not adhere to the rules of schema evolution
    ByteArrayOutputStream outStream = new ByteArrayOutputStream();

    EventSerializer serializer = createEventSerializer(outStream);
    serializer.afterCreate();

    ImagesVisible visEvent = new ImagesVisible(true,
        Arrays.asList((CharSequence) "acct1_vid1_thumb1", "acct1_vid2_thumb2"));

    Schema schema = null;
    GenericRecord record = null;
    JSONObject schemaJson = null;

    FileInputStream JSONFile = new FileInputStream(new File("src/test/java/com/neon/flume/TestEvent.avsc"));

    String JSONFileStr = IOUtils.toString(JSONFile, "UTF-8");

    File schemaFile = null;

    for (int i = 0; i < 10; i++) {
      if (i < 4) {
        schemaJson = new JSONObject(JSONFileStr);
        schemaFile = new File("src/test/java/com/neon/flume/TestEvent.avsc");
      } else {
        schemaJson = new JSONObject(TrackerEvent.getClassSchema().toString());
        schemaFile = new File("src/test/java/com/neon/flume/TrackerEvent.avsc");
      }

      schema = new Schema.Parser().parse(schemaJson.toString());

      record = buildDefaultGenericEvent(schema).set("eventType", "IMAGE_VISIBLE").set("eventData", visEvent).build();

      Event event = EventBuilder.withBody(serializeAvro(record, schema));
      event.getHeaders().put(NeonAvroEventSerializer.AVRO_SCHEMA_URL_HEADER,
          schemaFile.toURI().toURL().toExternalForm());
      serializer.write(event);
      schemaArray[i] = schema;
    }

    shutDownAll(serializer, outStream);
    validateAvroEvents(outStream);
  }

  @Test
  public void testBadSchema() throws IOException {
    // Test to ensure that the serializer fails properly when no schema is given
    ByteArrayOutputStream outStream = new ByteArrayOutputStream();

    Schema schema = null;
    GenericRecord record = null;

    String badSchema = "{\"type\":\"record\", \"name\":\"com.neon.Tracker.TrackerEvent\", "
        + "\"fields\":[{\"name\":\"pageUrl\", \"type\":\"string\"}]}";

    File schemaFile = new File("src/test/java/com/neon/flume/BadEvent.avsc");
    String header = schemaFile.toURI().toURL().toExternalForm();
    NeonAvroEventSerializer.URLOpener mockUrl = mock(NeonAvroEventSerializer.URLOpener.class);
    when(mockUrl.open(header)).thenReturn(new ByteArrayInputStream(badSchema.getBytes()));

    EventSerializer serializer = createMockedEventSerializer(outStream, mockUrl);
    serializer.afterCreate();

    schema = new Schema.Parser().parse(badSchema);

    record = new GenericRecordBuilder(schema).set("pageUrl", "Yes").build();
    for (int i = 0; i < 10; i++) {
      Event event = EventBuilder.withBody(serializeAvro(record, schema));
      event.getHeaders().put(NeonAvroEventSerializer.AVRO_SCHEMA_URL_HEADER, header);
      serializer.write(event);
      assertLogExists(Level.ERROR,
          "Error parsing avro event org.apache.avro.AvroTypeException: "
              + "Found com.neon.Tracker.TrackerEvent, expecting com.neon.Tracker.TrackerEvent, "
              + "missing required field pageId");
    }
    serializer.beforeClose();
  }

  @Test
  public void testConnectionError() throws IOException {
    // Test to ensure that the serializer fails gracefully when a connection
    // error occurs
    ByteArrayOutputStream outStream = new ByteArrayOutputStream();
    Schema schema = null;
    GenericRecord record = null;

    schema = TrackerEvent.getClassSchema();

    ImagesVisible visEvent = new ImagesVisible(true,
        Arrays.asList((CharSequence) "acct1_vid1_thumb1", "acct1_vid2_thumb2"));

    record = buildDefaultGenericEvent(schema).set("eventType", "IMAGE_VISIBLE").set("eventData", visEvent).build();

    File schemaFile = new File("src/test/java/com/neon/flume/TrackerEvent.avsc");

    NeonAvroEventSerializer.URLOpener mockUrl = mock(NeonAvroEventSerializer.URLOpener.class);
    EventSerializer serializer = createMockedEventSerializer(outStream, mockUrl);
    serializer.afterCreate();

    String headerData = schemaFile.toURI().toURL().toExternalForm();

    when(mockUrl.open(headerData)).thenThrow(new IOException("Connection Error"));

    Event event = EventBuilder.withBody(serializeAvro(record, schema));
    event.getHeaders().put(NeonAvroEventSerializer.AVRO_SCHEMA_URL_HEADER, headerData);

    serializer.write(event);

    assertLogExists(Level.ERROR, "Connection Error");
    serializer.beforeClose();
  }

  public EventSerializer createEventSerializer(OutputStream out) {
    // Create the evnet serializer
    Context ctx = new Context();
    EventSerializer.Builder builder = new NeonAvroEventSerializer.Builder();
    EventSerializer serializer = builder.build(ctx, out);
    return serializer;
  }

  public EventSerializer createMockedEventSerializer(OutputStream out, URLOpener urlOpener) {
    // Create the event serializer
    // This calls a different constructor so that mocking can be done
    Context ctx = new Context();
    EventSerializer serializer = new NeonAvroEventSerializer.Builder().build(ctx, out, urlOpener);
    return serializer;
  }

  public void shutDownAll(EventSerializer serializer, OutputStream out) throws IOException {
    // Close down the serializer and stream
    serializer.flush();
    serializer.beforeClose();
    out.flush();
    out.close();
  }

  private GenericRecordBuilder buildDefaultGenericEvent(Schema schema) {
    // Build a generic record
    return new GenericRecordBuilder(schema).set("pageId", new Utf8("pageId_dummy"))
        .set("trackerAccountId", "trackerAccountId_dummy").set("trackerType", "IGN").set("pageURL", "pageUrl_dummy")
        .set("refURL", "refUrl_dummy").set("serverTime", 1416612478000L).set("clientTime", 1416612478000L)
        .set("clientIP", "clientIp_dummy").set("neonUserId", "neonUserId_dummy").set("userAgent", "userAgentDummy")
        .set("agentInfo", null).set("ipGeoData", GeoData.newBuilder().setCity("Toronto").setCountry("CAN").setZip(null)
            .setRegion("ON").setLat(null).setLon(null).build());
  }

  private byte[] serializeAvro(Object datum, Schema schema) throws IOException {
    // Serialize an Avro object
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    ReflectDatumWriter<Object> writer = new ReflectDatumWriter<Object>(schema);
    BinaryEncoder encoder = EncoderFactory.get().binaryEncoder(out, null);
    out.reset();
    writer.write(datum, encoder);
    encoder.flush();
    return out.toByteArray();
  }

  public void validateAvroEvents(ByteArrayOutputStream out) throws IOException {
    // Reads the events from memory and checks them against a hardcoded record.
    // The record is what the expected output is supposed to be.
    // If they're the same, then it passes the test.
    byte buf[] = out.toByteArray();
    ByteArrayInputStream recordReader = new ByteArrayInputStream(buf);
    int numEvents = 0;
    DatumReader<GenericRecord> reader = new GenericDatumReader<GenericRecord>();
    DataFileStream<GenericRecord> streamReader = new DataFileStream<GenericRecord>(recordReader, reader);
    while (streamReader.hasNext()) {
      GenericRecord record = new GenericData.Record(schemaArray[numEvents]);
      Assert.assertTrue(streamReader.next(record).toString().startsWith(testRecord));
      numEvents++;
    }
    streamReader.close();
    Assert.assertEquals("Should have found a total of 10 events", 10, numEvents);
  }

  private void assertLogExists(Level level, String regex) {
    if (!testAppender.logExists(regex, level)) {
      fail("The expected log: " + regex + " was not found. Logs seen:\n" + testAppender.getLogs());
    }
  }
}
